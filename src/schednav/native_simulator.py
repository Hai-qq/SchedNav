"""Deterministic, first-party discrete-event GPU scheduling simulator."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from .contracts import canonical_sha256
from .native_trace import CanonicalTrace, TraceJob, load_canonical_trace


POLICY_SCHEMA = "schednav.simulation-policy/v1"
RESULT_SCHEMA = "schednav.simulation-result/v1"
METRICS_SCHEMA = "schednav.metrics-report/v2"
EPSILON = 1e-9


@dataclass(frozen=True)
class SimulationPolicy:
    action_id: str
    scheduler: str
    spot_guarantee_seconds: int
    checkpoint_interval_seconds: int
    preemption_overhead_seconds: int
    placement_strategy: str = "deterministic_best_fit"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SimulationPolicy":
        required = {
            "schema_version",
            "action_id",
            "scheduler",
            "spot_guarantee_seconds",
            "checkpoint_interval_seconds",
            "preemption_overhead_seconds",
            "placement_strategy",
        }
        if set(value) != required:
            raise ValueError(f"Simulation policy fields must be exactly {sorted(required)}")
        if value["schema_version"] != POLICY_SCHEMA:
            raise ValueError(f"Expected schema_version={POLICY_SCHEMA}")
        policy = cls(
            action_id=str(value["action_id"]),
            scheduler=str(value["scheduler"]),
            spot_guarantee_seconds=int(value["spot_guarantee_seconds"]),
            checkpoint_interval_seconds=int(value["checkpoint_interval_seconds"]),
            preemption_overhead_seconds=int(value["preemption_overhead_seconds"]),
            placement_strategy=str(value["placement_strategy"]),
        )
        policy.validate()
        return policy

    @classmethod
    def load(cls, path: Path) -> "SimulationPolicy":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def validate(self) -> None:
        if not self.action_id:
            raise ValueError("action_id cannot be empty")
        if self.scheduler not in {"fifo", "priority_preemptive"}:
            raise ValueError("scheduler must be fifo or priority_preemptive")
        if self.spot_guarantee_seconds < 0:
            raise ValueError("spot_guarantee_seconds cannot be negative")
        if self.checkpoint_interval_seconds <= 0:
            raise ValueError("checkpoint_interval_seconds must be positive")
        if self.preemption_overhead_seconds < 0:
            raise ValueError("preemption_overhead_seconds cannot be negative")
        if self.placement_strategy != "deterministic_best_fit":
            raise ValueError("Only deterministic_best_fit placement is supported")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(asdict(self))


@dataclass
class _JobState:
    job: TraceJob
    remaining: float
    queued_since: float
    enqueue_order: int
    first_start: float | None = None
    completion: float | None = None
    queue_seconds: float = 0.0
    preemptions: int = 0
    run_count: int = 0
    run_start: float | None = None
    allocation: dict[str, float] | None = None


def _allocate(
    job: TraceJob,
    nodes: dict[str, dict[str, Any]],
) -> dict[str, float] | None:
    eligible = [
        (node_id, float(node["free"]))
        for node_id, node in nodes.items()
        if node["free"] > 0 and (job.gpu_model == "*" or node["gpu_model"] == job.gpu_model)
    ]
    if sum(free for _, free in eligible) < job.gpu_count:
        return None
    eligible.sort(key=lambda item: (-item[1], item[0]))
    remaining = job.gpu_count
    allocation: dict[str, float] = {}
    for node_id, free in eligible:
        assigned = min(free, remaining)
        if assigned:
            allocation[node_id] = assigned
            remaining -= assigned
        if remaining == 0:
            break
    return allocation


def _reserve(allocation: dict[str, float], nodes: dict[str, dict[str, Any]]) -> None:
    for node_id, count in allocation.items():
        nodes[node_id]["free"] -= count


def _release(allocation: dict[str, float] | None, nodes: dict[str, dict[str, Any]]) -> None:
    for node_id, count in (allocation or {}).items():
        nodes[node_id]["free"] += count


def _eligible_free(job: TraceJob, nodes: dict[str, dict[str, Any]]) -> float:
    return sum(
        float(node["free"])
        for node in nodes.values()
        if job.gpu_model == "*" or node["gpu_model"] == job.gpu_model
    )


def _shares_eligible_model(job: TraceJob, state: _JobState, nodes: dict[str, dict[str, Any]]) -> bool:
    return any(
        count > 0 and (job.gpu_model == "*" or nodes[node_id]["gpu_model"] == job.gpu_model)
        for node_id, count in (state.allocation or {}).items()
    )


def simulate_trace(trace: CanonicalTrace, policy: SimulationPolicy) -> dict[str, Any]:
    """Run one deterministic, drain-to-completion counterfactual simulation."""
    policy.validate()
    nodes = {
        node.node_id: {
            "gpu_model": node.gpu_model,
            "capacity": node.gpu_count,
            "free": node.gpu_count,
        }
        for node in trace.nodes
    }
    states = {
        job.job_id: _JobState(job, job.duration_seconds, job.submit_time_seconds, index)
        for index, job in enumerate(trace.jobs)
    }
    arrivals = list(trace.jobs)
    arrival_index = 0
    queue: list[str] = []
    running: set[str] = set()
    completed: set[str] = set()
    preemption_events: list[dict[str, Any]] = []
    spot_runs: list[dict[str, Any]] = []
    next_enqueue_order = len(arrivals)
    evaluation_start = min(job.submit_time_seconds for job in arrivals)
    evaluation_end = max(job.submit_time_seconds for job in arrivals)
    now = evaluation_start
    simulation_start = now
    allocated_gpu_seconds = 0.0
    evaluation_allocated_gpu_seconds = 0.0

    def queue_key(job_id: str) -> tuple[Any, ...]:
        state = states[job_id]
        if policy.scheduler == "priority_preemptive":
            priority = 0 if state.job.service_class == "HP" else 1
            return (priority, state.enqueue_order, state.job.job_id)
        return (state.enqueue_order, state.job.job_id)

    def close_spot_run(state: _JobState, end_time: float, outcome: str) -> None:
        if state.job.service_class != "Spot" or state.run_start is None:
            return
        uninterrupted = end_time - state.run_start
        success = outcome == "completed" or uninterrupted + EPSILON >= policy.spot_guarantee_seconds
        spot_runs.append(
            {
                "job_id": state.job.job_id,
                "run_ordinal": state.run_count,
                "start_time_seconds": state.run_start,
                "end_time_seconds": end_time,
                "end_reason": outcome,
                "uninterrupted_seconds": uninterrupted,
                "guarantee_seconds": policy.spot_guarantee_seconds,
                "guarantee_succeeded": success,
            }
        )

    def start_jobs() -> bool:
        started_any = False
        while True:
            started = False
            for job_id in sorted(queue, key=queue_key):
                state = states[job_id]
                allocation = _allocate(state.job, nodes)
                if allocation is None:
                    continue
                queue.remove(job_id)
                _reserve(allocation, nodes)
                state.queue_seconds += now - state.queued_since
                state.first_start = now if state.first_start is None else state.first_start
                state.run_start = now
                state.run_count += 1
                state.allocation = allocation
                running.add(job_id)
                started = True
                started_any = True
                break
            if not started:
                return started_any

    def preempt_for_hp() -> bool:
        nonlocal next_enqueue_order
        if policy.scheduler != "priority_preemptive":
            return False
        changed = False
        for pending_id in sorted(queue, key=queue_key):
            pending = states[pending_id]
            if pending.job.service_class != "HP":
                continue
            while _eligible_free(pending.job, nodes) < pending.job.gpu_count:
                candidates = [
                    states[job_id]
                    for job_id in running
                    if states[job_id].job.service_class == "Spot"
                    and states[job_id].run_start is not None
                    and now + EPSILON
                    >= float(states[job_id].run_start) + policy.spot_guarantee_seconds
                    and _shares_eligible_model(pending.job, states[job_id], nodes)
                ]
                if not candidates:
                    break
                candidates.sort(
                    key=lambda state: (
                        state.remaining,
                        state.job.submit_time_seconds,
                        state.job.job_id,
                    ),
                    reverse=True,
                )
                victim = candidates[0]
                run_seconds = now - float(victim.run_start)
                rollback = run_seconds % policy.checkpoint_interval_seconds
                overhead = float(policy.preemption_overhead_seconds)
                victim.remaining += rollback + overhead
                close_spot_run(victim, now, "preempted")
                preemption_events.append(
                    {
                        "time_seconds": now,
                        "preempted_job_id": victim.job.job_id,
                        "triggering_job_id": pending.job.job_id,
                        "rollback_seconds": rollback,
                        "overhead_seconds": overhead,
                        "added_gpu_seconds": (rollback + overhead) * victim.job.gpu_count,
                    }
                )
                _release(victim.allocation, nodes)
                victim.allocation = None
                victim.run_start = None
                victim.preemptions += 1
                victim.queued_since = now
                victim.enqueue_order = next_enqueue_order
                next_enqueue_order += 1
                running.remove(victim.job.job_id)
                queue.append(victim.job.job_id)
                changed = True
        return changed

    while len(completed) < len(states):
        while arrival_index < len(arrivals) and arrivals[arrival_index].submit_time_seconds <= now + EPSILON:
            queue.append(arrivals[arrival_index].job_id)
            arrival_index += 1
        start_jobs()
        if preempt_for_hp():
            start_jobs()

        event_times: list[float] = []
        if arrival_index < len(arrivals):
            event_times.append(arrivals[arrival_index].submit_time_seconds)
        event_times.extend(now + states[job_id].remaining for job_id in running)
        if policy.scheduler == "priority_preemptive" and any(
            states[job_id].job.service_class == "HP" for job_id in queue
        ):
            event_times.extend(
                float(states[job_id].run_start) + policy.spot_guarantee_seconds
                for job_id in running
                if states[job_id].job.service_class == "Spot"
                and states[job_id].run_start is not None
                and float(states[job_id].run_start) + policy.spot_guarantee_seconds > now + EPSILON
            )
        future = [value for value in event_times if value > now + EPSILON]
        if not future:
            pending = sorted(set(queue) | running)
            raise RuntimeError(f"Simulation cannot make progress; pending jobs={pending}")
        next_time = min(future)
        delta = next_time - now
        allocated = sum(states[job_id].job.gpu_count for job_id in running)
        allocated_gpu_seconds += allocated * delta
        evaluation_overlap = max(
            0.0,
            min(next_time, evaluation_end) - max(now, evaluation_start),
        )
        evaluation_allocated_gpu_seconds += allocated * evaluation_overlap
        for job_id in running:
            states[job_id].remaining = max(0.0, states[job_id].remaining - delta)
        now = next_time

        ending = sorted(
            [job_id for job_id in running if states[job_id].remaining <= EPSILON]
        )
        for job_id in ending:
            state = states[job_id]
            close_spot_run(state, now, "completed")
            _release(state.allocation, nodes)
            state.allocation = None
            state.run_start = None
            state.completion = now
            running.remove(job_id)
            completed.add(job_id)

    simulation_end = now
    result: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "engine": {"name": "schednav-sim", "version": "1"},
        "trace": {
            "trace_id": trace.trace_id,
            "trace_fingerprint": trace.fingerprint,
            "time_origin": trace.time_origin,
            "source": trace.source,
        },
        "policy": asdict(policy),
        "policy_fingerprint": policy.fingerprint,
        "window_seconds": {
            "evaluation_start": evaluation_start,
            "evaluation_end": evaluation_end,
            "drain_end": simulation_end,
        },
        "cluster": {
            "node_count": len(nodes),
            "capacity_gpus": trace.capacity_gpus,
            "simulation_start": simulation_start,
            "simulation_end": simulation_end,
            "allocated_gpu_seconds": allocated_gpu_seconds,
            "evaluation_allocated_gpu_seconds": evaluation_allocated_gpu_seconds,
            "drain_allocated_gpu_seconds": (
                allocated_gpu_seconds - evaluation_allocated_gpu_seconds
            ),
            "allocation_rate_mean": (
                evaluation_allocated_gpu_seconds
                / (trace.capacity_gpus * (evaluation_end - evaluation_start))
                if evaluation_end > evaluation_start
                else 0.0
            ),
        },
        "jobs": [
            {
                "job_id": state.job.job_id,
                "service_class": state.job.service_class,
                "submit_time_seconds": state.job.submit_time_seconds,
                "start_time_seconds": state.first_start,
                "completion_time_seconds": state.completion,
                "duration_seconds": state.job.duration_seconds,
                "gpu_count": state.job.gpu_count,
                "gpu_model": state.job.gpu_model,
                "queue_seconds": state.queue_seconds,
                "jct_seconds": float(state.completion) - state.job.submit_time_seconds,
                "preemption_count": state.preemptions,
                "run_count": state.run_count,
            }
            for state in sorted(states.values(), key=lambda item: item.job.job_id)
        ],
        "preemption_events": preemption_events,
        "spot_runs": spot_runs,
    }
    result["result_fingerprint"] = canonical_sha256(result)
    return result


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _summary(values: list[float]) -> dict[str, float | None]:
    return {
        "mean": round(sum(values) / len(values), 6) if values else None,
        "p50": round(value, 6) if (value := _quantile(values, 0.50)) is not None else None,
        "p95": round(value, 6) if (value := _quantile(values, 0.95)) is not None else None,
    }


def build_metrics_report(result: dict[str, Any]) -> dict[str, Any]:
    supplied = result.get("result_fingerprint")
    payload = {key: value for key, value in result.items() if key != "result_fingerprint"}
    if result.get("schema_version") != RESULT_SCHEMA or canonical_sha256(payload) != supplied:
        raise ValueError("A valid SchedNav simulation result is required")
    jobs_by_type: dict[str, Any] = {}
    for service_class in ("HP", "Spot"):
        selected = [job for job in result["jobs"] if job["service_class"] == service_class]
        preemptions = [int(job["preemption_count"]) for job in selected]
        jobs_by_type[service_class] = {
            "job_count": len(selected),
            "completed_count": sum(job["completion_time_seconds"] is not None for job in selected),
            "completion_rate": 1.0 if selected else None,
            "jct_seconds": _summary([float(job["jct_seconds"]) for job in selected]),
            "queue_seconds": _summary([float(job["queue_seconds"]) for job in selected]),
            "preemption_count": sum(preemptions),
            "preempted_job_count": sum(value > 0 for value in preemptions),
            "preempted_job_rate": (
                sum(value > 0 for value in preemptions) / len(selected) if selected else None
            ),
        }
    events = result["preemption_events"]
    spot_runs = result["spot_runs"]
    successful_guarantees = sum(bool(run["guarantee_succeeded"]) for run in spot_runs)
    failed_guarantees = len(spot_runs) - successful_guarantees
    spot_preemption_count = jobs_by_type["Spot"]["preemption_count"]
    preempted_ids = {event["preempted_job_id"] for event in events}
    report: dict[str, Any] = {
        "schema_version": METRICS_SCHEMA,
        "run_spec_fingerprint": result["result_fingerprint"],
        "policy_fingerprint": result["policy_fingerprint"],
        "policy": result["policy"],
        "source": {
            "engine": result["engine"],
            "trace_fingerprint": result["trace"]["trace_fingerprint"],
            "dataset": result["trace"]["source"].get("dataset", result["trace"]["trace_id"]),
        },
        "trace_id": result["trace"]["trace_id"],
        "window_seconds": {
            "evaluation_start": result["window_seconds"]["evaluation_start"],
            "evaluation_end": result["window_seconds"]["evaluation_end"],
        },
        "jobs": jobs_by_type,
        "cluster": result["cluster"],
        "preemption_events": {
            "available": True,
            "event_count": len(events),
            "counted_spot_failure_count": len(events),
            "preempted_job_count": len(preempted_ids),
            "events_during_evaluation_window": sum(
                event["time_seconds"] <= result["window_seconds"]["evaluation_end"]
                for event in events
            ),
            "events_during_drain": sum(
                event["time_seconds"] > result["window_seconds"]["evaluation_end"]
                for event in events
            ),
            "rollback_seconds_total": sum(float(event["rollback_seconds"]) for event in events),
            "overhead_seconds_total": sum(float(event["overhead_seconds"]) for event in events),
            "added_gpu_seconds_total": sum(float(event["added_gpu_seconds"]) for event in events),
            "consistent_with_job_csv": len(events) == spot_preemption_count,
            "spot_run_count": len(spot_runs),
            "eviction_rate_per_run": len(events) / len(spot_runs) if spot_runs else None,
        },
        "spot_runs": {
            "available": True,
            "event_count": len(spot_runs),
            "events_during_evaluation_window": sum(
                run["start_time_seconds"] <= result["window_seconds"]["evaluation_end"]
                for run in spot_runs
            ),
            "events_during_drain": sum(
                run["start_time_seconds"] > result["window_seconds"]["evaluation_end"]
                for run in spot_runs
            ),
            "consistent_with_job_csv": len(spot_runs)
            == sum(job["run_count"] for job in result["jobs"] if job["service_class"] == "Spot"),
        },
        "spot_guarantee": {
            "available": True,
            "event_count": len(spot_runs),
            "succeeded_count": successful_guarantees,
            "failed_count": failed_guarantees,
            "success_rate": successful_guarantees / len(spot_runs) if spot_runs else None,
            "consistent_with_preemption_events": failed_guarantees <= len(events)
            and all(
                bool(run["guarantee_succeeded"]) or run["end_reason"] == "preempted"
                for run in spot_runs
            ),
        },
        "evidence": {"simulation_result_fingerprint": result["result_fingerprint"]},
        "definitions": {
            "allocation_rate_mean": "Allocated GPU-seconds divided by physical GPU-seconds inside the evaluation arrival window; drain is excluded from this utilization denominator.",
            "spot_eviction_rate_per_run": "Spot preemption events divided by explicit Spot run starts.",
            "spot_guarantee_success_rate": "Spot runs completing or remaining uninterrupted for the configured guarantee duration, divided by all Spot runs.",
        },
    }
    report["metrics_fingerprint"] = canonical_sha256(report)
    return report


def run_native_simulation(trace_path: Path, policy_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    result = simulate_trace(load_canonical_trace(trace_path), SimulationPolicy.load(policy_path))
    return result, build_metrics_report(result)
