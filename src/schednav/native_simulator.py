"""Deterministic, first-party discrete-event GPU scheduling simulator."""

from __future__ import annotations

from bisect import insort_right
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from typing import Any

from .contracts import canonical_sha256
from .controller_factory import (
    PredictiveController,
    create_predictive_controller,
    load_controller_config,
)
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
    hp_preemption_delay_seconds: int = 0
    spot_eviction_budget_rate: float | None = None
    preemption_victim_strategy: str = "longest_remaining"
    predictive_admission_mode: str = "enforce"

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
        optional = {
            "hp_preemption_delay_seconds",
            "spot_eviction_budget_rate",
            "preemption_victim_strategy",
            "predictive_admission_mode",
        }
        if not required.issubset(value) or not set(value).issubset(required | optional):
            raise ValueError(
                f"Simulation policy fields must contain {sorted(required)} and only allow {sorted(optional)}"
            )
        if value["schema_version"] != POLICY_SCHEMA:
            raise ValueError(f"Expected schema_version={POLICY_SCHEMA}")
        policy = cls(
            action_id=str(value["action_id"]),
            scheduler=str(value["scheduler"]),
            spot_guarantee_seconds=int(value["spot_guarantee_seconds"]),
            checkpoint_interval_seconds=int(value["checkpoint_interval_seconds"]),
            preemption_overhead_seconds=int(value["preemption_overhead_seconds"]),
            placement_strategy=str(value["placement_strategy"]),
            hp_preemption_delay_seconds=int(
                value.get("hp_preemption_delay_seconds", 0)
            ),
            spot_eviction_budget_rate=(
                float(value["spot_eviction_budget_rate"])
                if value.get("spot_eviction_budget_rate") is not None
                else None
            ),
            preemption_victim_strategy=str(
                value.get("preemption_victim_strategy", "longest_remaining")
            ),
            predictive_admission_mode=str(
                value.get("predictive_admission_mode", "enforce")
            ),
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
        if self.hp_preemption_delay_seconds < 0:
            raise ValueError("hp_preemption_delay_seconds cannot be negative")
        if self.spot_eviction_budget_rate is not None and not (
            0.0 <= self.spot_eviction_budget_rate <= 1.0
        ):
            raise ValueError("spot_eviction_budget_rate must be between 0 and 1")
        if self.preemption_victim_strategy not in {
            "longest_remaining",
            "lowest_checkpoint_loss",
        }:
            raise ValueError(
                "preemption_victim_strategy must be longest_remaining or "
                "lowest_checkpoint_loss"
            )
        if self.predictive_admission_mode not in {"enforce", "bypass"}:
            raise ValueError(
                "predictive_admission_mode must be enforce or bypass"
            )
        if self.placement_strategy != "deterministic_best_fit":
            raise ValueError("Only deterministic_best_fit placement is supported")

    def to_dict(self) -> dict[str, Any]:
        """Return a backward-compatible policy payload for hashing and evidence."""
        value = asdict(self)
        if self.hp_preemption_delay_seconds == 0:
            value.pop("hp_preemption_delay_seconds")
        if self.spot_eviction_budget_rate is None:
            value.pop("spot_eviction_budget_rate")
        if self.preemption_victim_strategy == "longest_remaining":
            value.pop("preemption_victim_strategy")
        if self.predictive_admission_mode == "enforce":
            value.pop("predictive_admission_mode")
        return value

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self.to_dict())


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
    feedback_checkpoint_time: float | None = None
    allocation: dict[str, float] | None = None
    run_guarantee_seconds: int | None = None


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


def simulate_trace(
    trace: CanonicalTrace,
    policy: SimulationPolicy,
    predictive_controller: PredictiveController | None = None,
    rolling_controller: Any | None = None,
    *,
    initial_queue_wait_seconds_by_job_id: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Run one deterministic, drain-to-completion counterfactual simulation.

    A rolling controller may switch only the registered high-level policy at its
    declared cutoffs.  The simulator state itself is never rebuilt: queues,
    allocations, remaining work, ledgers, and predictive-controller state flow
    across every decision boundary in the same event loop.
    """
    policy.validate()
    active_policy = policy
    nodes = {
        node.node_id: {
            "gpu_model": node.gpu_model,
            "capacity": node.gpu_count,
            "free": node.gpu_count,
        }
        for node in trace.nodes
    }
    free_by_model: dict[str, float] = {}
    for node in nodes.values():
        model = str(node["gpu_model"])
        free_by_model[model] = free_by_model.get(model, 0.0) + float(node["free"])
    total_free = sum(free_by_model.values())
    initial_queue_wait = {
        str(job_id): float(seconds)
        for job_id, seconds in (initial_queue_wait_seconds_by_job_id or {}).items()
    }
    known_job_ids = {job.job_id for job in trace.jobs}
    if not set(initial_queue_wait).issubset(known_job_ids):
        raise ValueError("Initial queue-wait state names an unknown job")
    if any(
        not math.isfinite(seconds) or seconds < 0
        for seconds in initial_queue_wait.values()
    ):
        raise ValueError("Initial queue-wait seconds must be finite and non-negative")
    states = {
        job.job_id: _JobState(
            job,
            job.duration_seconds,
            job.submit_time_seconds - initial_queue_wait.get(job.job_id, 0.0),
            index,
        )
        for index, job in enumerate(trace.jobs)
    }
    arrivals = list(trace.jobs)
    arrival_index = 0
    queue: list[str] = []
    queued_hp_count = 0
    running: set[str] = set()
    completed: set[str] = set()
    preemption_events: list[dict[str, Any]] = []
    spot_runs: list[dict[str, Any]] = []
    outstanding_hp_requested_gpus = 0.0
    next_enqueue_order = len(arrivals)
    simulation_start = min(job.submit_time_seconds for job in arrivals)
    evaluation_start = (
        trace.evaluation_start_seconds
        if trace.evaluation_start_seconds is not None
        else simulation_start
    )
    evaluation_end = (
        trace.evaluation_end_seconds
        if trace.evaluation_end_seconds is not None
        else max(job.submit_time_seconds for job in arrivals)
    )
    if predictive_controller is not None:
        predictive_controller.bind_evidence_window(evaluation_start, evaluation_end)
        predictive_controller.quota_for_guarantee_seconds(
            policy.spot_guarantee_seconds
        )
    if rolling_controller is not None:
        rolling_controller.bind_evidence_window(evaluation_start, evaluation_end)
    evaluation_spot_ids = {
        job.job_id
        for job in arrivals
        if job.service_class == "Spot"
        and evaluation_start <= job.submit_time_seconds <= evaluation_end
    }
    now = simulation_start
    simulation_start = now
    allocated_gpu_seconds = 0.0
    warmup_allocated_gpu_seconds = 0.0
    evaluation_allocated_gpu_seconds = 0.0
    latest_predictive_observation: dict[str, Any] | None = None

    def enqueue(job_id: str) -> None:
        nonlocal queued_hp_count
        insort_right(queue, job_id, key=queue_key)
        if states[job_id].job.service_class == "HP":
            queued_hp_count += 1

    def eligible_free(job: TraceJob) -> float:
        if job.gpu_model == "*":
            return total_free
        return free_by_model.get(job.gpu_model, 0.0)

    def reserve(allocation: dict[str, float]) -> None:
        nonlocal total_free
        _reserve(allocation, nodes)
        for node_id, count in allocation.items():
            model = str(nodes[node_id]["gpu_model"])
            free_by_model[model] -= count
            total_free -= count

    def release(allocation: dict[str, float] | None) -> None:
        nonlocal total_free
        _release(allocation, nodes)
        for node_id, count in (allocation or {}).items():
            model = str(nodes[node_id]["gpu_model"])
            free_by_model[model] += count
            total_free += count

    def queue_key(job_id: str) -> tuple[Any, ...]:
        state = states[job_id]
        if active_policy.scheduler == "priority_preemptive":
            priority = 0 if state.job.service_class == "HP" else 1
            return (priority, state.enqueue_order, state.job.job_id)
        return (state.enqueue_order, state.job.job_id)

    def record_controller_feedback(
        state: _JobState,
        event_time: float,
        *,
        evicted: bool,
        event_kind: str,
    ) -> None:
        if predictive_controller is None or state.job.service_class != "Spot":
            return
        allocation = state.allocation or {}
        allocations_by_pool: dict[str, list[str]] = {}
        for node_id, count in allocation.items():
            if count > 0:
                pool = str(nodes[node_id]["gpu_model"])
                allocations_by_pool.setdefault(pool, []).append(node_id)
        guarantee_seconds = (
            state.run_guarantee_seconds
            if state.run_guarantee_seconds is not None
            else active_policy.spot_guarantee_seconds
        )
        if getattr(predictive_controller, "resource_pool_scoped", False):
            for pool, node_ids in allocations_by_pool.items():
                predictive_controller.observe_spot_run_end(
                    event_time,
                    evicted=evicted,
                    resource_pool=pool,
                    event_weight=float(len(node_ids)),
                    guarantee_seconds=guarantee_seconds,
                    event_kind=event_kind,
                )
        else:
            predictive_controller.observe_spot_run_end(
                event_time,
                evicted=evicted,
                event_weight=float(max(1, len(allocation))),
                guarantee_seconds=guarantee_seconds,
                event_kind=event_kind,
            )

    def close_spot_run(state: _JobState, end_time: float, outcome: str) -> None:
        if state.job.service_class != "Spot" or state.run_start is None:
            return
        uninterrupted = end_time - state.run_start
        guarantee_seconds = (
            state.run_guarantee_seconds
            if state.run_guarantee_seconds is not None
            else active_policy.spot_guarantee_seconds
        )
        success = outcome == "completed" or uninterrupted + EPSILON >= guarantee_seconds
        spot_runs.append(
            {
                "job_id": state.job.job_id,
                "run_ordinal": state.run_count,
                "start_time_seconds": state.run_start,
                "end_time_seconds": end_time,
                "end_reason": outcome,
                "uninterrupted_seconds": uninterrupted,
                "guarantee_seconds": guarantee_seconds,
                "guarantee_succeeded": success,
            }
        )
        record_controller_feedback(
            state,
            end_time,
            evicted=outcome == "preempted",
            event_kind="preempted" if outcome == "preempted" else "job_completed",
        )

    def running_spot_gpus() -> float:
        return sum(
            sum((states[job_id].allocation or {}).values())
            for job_id in running
            if states[job_id].job.service_class == "Spot"
        )

    def running_spot_gpus_by_pool() -> dict[str, float]:
        result: dict[str, float] = {}
        for job_id in running:
            state = states[job_id]
            if state.job.service_class != "Spot":
                continue
            for node_id, count in (state.allocation or {}).items():
                pool = str(nodes[node_id]["gpu_model"])
                result[pool] = result.get(pool, 0.0) + float(count)
        return result

    def start_jobs() -> bool:
        nonlocal queued_hp_count
        if total_free <= EPSILON:
            return False
        started_ids: list[str] = []
        for job_id in list(queue):
            if total_free <= EPSILON:
                break
            state = states[job_id]
            if (
                predictive_controller is not None
                and state.job.service_class == "Spot"
                and active_policy.predictive_admission_mode == "enforce"
                and not (
                    getattr(predictive_controller, "control_window_only", False)
                    and (
                        now < evaluation_start - EPSILON
                        or now >= evaluation_end - EPSILON
                    )
                )
                and not predictive_controller.allows_spot(
                    state.job.gpu_count,
                    (
                        running_spot_gpus_by_pool().get(state.job.gpu_model, 0.0)
                        if getattr(
                            predictive_controller, "resource_pool_scoped", False
                        )
                        and state.job.gpu_model != "*"
                        else running_spot_gpus()
                    ),
                    active_policy.spot_guarantee_seconds,
                    resource_pool=state.job.gpu_model,
                )
            ):
                continue
            if eligible_free(state.job) < state.job.gpu_count:
                continue
            allocation = _allocate(state.job, nodes)
            if allocation is None:
                continue
            reserve(allocation)
            state.queue_seconds += now - state.queued_since
            state.first_start = now if state.first_start is None else state.first_start
            state.run_start = now
            state.feedback_checkpoint_time = (
                now if state.job.service_class == "Spot" else None
            )
            state.run_count += 1
            state.allocation = allocation
            state.run_guarantee_seconds = (
                active_policy.spot_guarantee_seconds
                if state.job.service_class == "Spot"
                else None
            )
            running.add(job_id)
            started_ids.append(job_id)
        if started_ids:
            started = set(started_ids)
            queued_hp_count -= sum(
                states[job_id].job.service_class == "HP" for job_id in started_ids
            )
            queue[:] = [job_id for job_id in queue if job_id not in started]
        return bool(started_ids)

    def preempt_for_hp() -> bool:
        nonlocal next_enqueue_order
        if active_policy.scheduler != "priority_preemptive" or queued_hp_count == 0:
            return False
        changed = False
        candidate_cache: dict[str, list[_JobState]] = {}
        candidate_offsets: dict[str, int] = {}
        for pending_id in list(queue):
            pending = states[pending_id]
            if pending.job.service_class != "HP":
                continue
            if (
                now + EPSILON
                < pending.queued_since + active_policy.hp_preemption_delay_seconds
            ):
                continue
            if eligible_free(pending.job) >= pending.job.gpu_count:
                continue
            model = pending.job.gpu_model
            if model not in candidate_cache:
                eligible_victims = [
                    states[job_id]
                    for job_id in running
                    if states[job_id].job.service_class == "Spot"
                    and states[job_id].run_start is not None
                    and now + EPSILON
                    >= float(states[job_id].run_start)
                    + float(states[job_id].run_guarantee_seconds or 0)
                    and _shares_eligible_model(pending.job, states[job_id], nodes)
                ]
                if active_policy.preemption_victim_strategy == "lowest_checkpoint_loss":
                    candidate_cache[model] = sorted(
                        eligible_victims,
                        key=lambda state: (
                            (now - float(state.run_start))
                            % active_policy.checkpoint_interval_seconds,
                            -state.remaining,
                            state.job.submit_time_seconds,
                            state.job.job_id,
                        ),
                    )
                else:
                    candidate_cache[model] = sorted(
                        eligible_victims,
                        key=lambda state: (
                            state.remaining,
                            state.job.submit_time_seconds,
                            state.job.job_id,
                        ),
                        reverse=True,
                    )
                candidate_offsets[model] = 0
            candidates = candidate_cache[model]
            offset = candidate_offsets[model]
            while eligible_free(pending.job) < pending.job.gpu_count:
                while (
                    offset < len(candidates)
                    and candidates[offset].job.job_id not in running
                ):
                    offset += 1
                if offset >= len(candidates):
                    break
                victim = candidates[offset]
                offset += 1
                if active_policy.spot_eviction_budget_rate is not None:
                    all_spot_run_starts = sum(
                        state.run_count
                        for state in states.values()
                        if state.job.service_class == "Spot"
                    )
                    all_projected_rate = (
                        (len(preemption_events) + 1) / (all_spot_run_starts + 1)
                    )
                    if all_projected_rate > active_policy.spot_eviction_budget_rate + EPSILON:
                        break
                    evaluation_spot_run_starts = sum(
                        state.run_count
                        for state in states.values()
                        if state.job.job_id in evaluation_spot_ids
                    )
                    evaluation_preemptions = sum(
                        event["preempted_job_id"] in evaluation_spot_ids
                        for event in preemption_events
                    )
                    if victim.job.job_id in evaluation_spot_ids:
                        evaluation_projected_rate = (
                            (evaluation_preemptions + 1)
                            / (evaluation_spot_run_starts + 1)
                        )
                        if (
                            evaluation_projected_rate
                            > active_policy.spot_eviction_budget_rate + EPSILON
                        ):
                            break
                run_seconds = now - float(victim.run_start)
                rollback = run_seconds % active_policy.checkpoint_interval_seconds
                overhead = float(active_policy.preemption_overhead_seconds)
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
                release(victim.allocation)
                victim.allocation = None
                victim.run_start = None
                victim.feedback_checkpoint_time = None
                victim.run_guarantee_seconds = None
                victim.preemptions += 1
                victim.queued_since = now
                victim.enqueue_order = next_enqueue_order
                next_enqueue_order += 1
                running.remove(victim.job.job_id)
                enqueue(victim.job.job_id)
                changed = True
            candidate_offsets[model] = offset
        return changed

    def build_rolling_snapshot() -> dict[str, Any]:
        visible_jobs = arrivals[:arrival_index]
        completed_evaluation_hp = [
            states[job_id]
            for job_id in completed
            if states[job_id].job.service_class == "HP"
            and evaluation_start
            <= states[job_id].job.submit_time_seconds
            <= now + EPSILON
        ]
        carryover = []
        private_handoff_state = []
        for job_id in sorted(set(queue) | running):
            state = states[job_id]
            carryover.append(
                {
                    "job_id": state.job.job_id,
                    "service_class": state.job.service_class,
                    "gpu_count": state.job.gpu_count,
                    "gpu_model": state.job.gpu_model,
                    "tenant_id": state.job.tenant_id,
                    "status": "running" if job_id in running else "queued",
                    "current_queue_wait_seconds": (
                        now - state.queued_since if job_id in queue else 0.0
                    ),
                    "current_run_elapsed_seconds": (
                        now - float(state.run_start)
                        if state.run_start is not None
                        else 0.0
                    ),
                    "run_guarantee_seconds": state.run_guarantee_seconds,
                    "allocated_gpus": sum((state.allocation or {}).values()),
                }
            )
            private_handoff_state.append(
                {
                    "job_id": state.job.job_id,
                    "remaining_seconds": state.remaining,
                    "queued_since_seconds": state.queued_since,
                    "accumulated_queue_seconds": state.queue_seconds,
                    "run_start_seconds": state.run_start,
                    "allocation": dict(sorted((state.allocation or {}).items())),
                    "preemptions": state.preemptions,
                    "run_count": state.run_count,
                }
            )
        snapshot: dict[str, Any] = {
            "schema_version": "schednav.scheduler-state-snapshot/v1",
            "cutoff_time_seconds": float(now),
            "trace_id": trace.trace_id,
            "visible_prefix_fingerprint": canonical_sha256(
                [
                    {
                        "job_id": job.job_id,
                        "submit_time_seconds": job.submit_time_seconds,
                        "duration_seconds": job.duration_seconds,
                        "gpu_count": job.gpu_count,
                        "service_class": job.service_class,
                        "gpu_model": job.gpu_model,
                        "tenant_id": job.tenant_id,
                    }
                    for job in visible_jobs
                ]
            ),
            "information_boundary": {
                "maximum_visible_submit_time_seconds": (
                    max(job.submit_time_seconds for job in visible_jobs)
                    if visible_jobs
                    else None
                ),
                "future_arrivals_visible": False,
                "visible_arrival_count": len(visible_jobs),
            },
            "cluster": {
                "capacity_gpus": trace.capacity_gpus,
                "free_gpus": total_free,
                "free_gpus_by_pool": dict(sorted(free_by_model.items())),
            },
            "queue": {
                "job_count": len(queue),
                "hp_job_count": queued_hp_count,
                "spot_job_count": len(queue) - queued_hp_count,
                "requested_gpus": sum(states[job_id].job.gpu_count for job_id in queue),
                "hp_requested_gpus": sum(
                    states[job_id].job.gpu_count
                    for job_id in queue
                    if states[job_id].job.service_class == "HP"
                ),
                "spot_requested_gpus": sum(
                    states[job_id].job.gpu_count
                    for job_id in queue
                    if states[job_id].job.service_class == "Spot"
                ),
                "maximum_hp_wait_seconds": max(
                    (
                        states[job_id].queue_seconds
                        + now
                        - states[job_id].queued_since
                        for job_id in queue
                        if states[job_id].job.service_class == "HP"
                    ),
                    default=0.0,
                ),
                "maximum_spot_wait_seconds": max(
                    (
                        states[job_id].queue_seconds
                        + now
                        - states[job_id].queued_since
                        for job_id in queue
                        if states[job_id].job.service_class == "Spot"
                    ),
                    default=0.0,
                ),
            },
            "running": {
                "job_count": len(running),
                "spot_gpus": running_spot_gpus(),
                "spot_gpus_by_pool": dict(sorted(running_spot_gpus_by_pool().items())),
            },
            "active_policy_action_id": active_policy.action_id,
            "active_policy_fingerprint": active_policy.fingerprint,
            "state_handoff_fingerprint": canonical_sha256(private_handoff_state),
            "carryover_jobs": carryover,
            "ledger_counts": {
                "completed_jobs": len(completed),
                "preemption_events": len(preemption_events),
                "spot_runs": len(spot_runs),
            },
            "slo_progress": {
                "cutoff_safe": True,
                "hp_completed_job_count": len(completed_evaluation_hp),
                "hp_completed_queue_seconds": sorted(
                    round(float(state.queue_seconds), 6)
                    for state in completed_evaluation_hp
                ),
                "hp_completed_jct_seconds": sorted(
                    round(
                        float(state.completion) - state.job.submit_time_seconds,
                        6,
                    )
                    for state in completed_evaluation_hp
                    if state.completion is not None
                ),
            },
        }
        snapshot["snapshot_fingerprint"] = canonical_sha256(snapshot)
        return snapshot

    while len(completed) < len(states):
        while arrival_index < len(arrivals) and arrivals[arrival_index].submit_time_seconds <= now + EPSILON:
            arriving = arrivals[arrival_index]
            enqueue(arriving.job_id)
            if arriving.service_class == "HP":
                outstanding_hp_requested_gpus += arriving.gpu_count
            arrival_index += 1
        if predictive_controller is not None and predictive_controller.is_update_due(now):
            hp_running_by_series: dict[str, float] = {}
            demand_series_metadata: dict[str, dict[str, str]] = {}
            for job_id in running:
                state = states[job_id]
                if state.job.service_class != "HP":
                    continue
                tenant = state.job.tenant_id or "__aggregate__"
                series = json.dumps(
                    [state.job.gpu_model, tenant],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                hp_running_by_series[series] = (
                    hp_running_by_series.get(series, 0.0) + state.job.gpu_count
                )
                demand_series_metadata[series] = {
                    "pool": state.job.gpu_model,
                    "cluster": "cluster",
                    "tenant": tenant,
                }
            spot_backlog_by_pool: dict[str, float] = {}
            maximum_spot_wait_by_pool: dict[str, float] = {}
            for job_id in queue:
                state = states[job_id]
                if state.job.service_class != "Spot":
                    continue
                pool = state.job.gpu_model
                spot_backlog_by_pool[pool] = (
                    spot_backlog_by_pool.get(pool, 0.0) + state.job.gpu_count
                )
                maximum_spot_wait_by_pool[pool] = max(
                    maximum_spot_wait_by_pool.get(pool, 0.0),
                    now - state.queued_since,
                )
            running_by_pool = running_spot_gpus_by_pool()
            latest_predictive_observation = predictive_controller.update(
                now,
                hp_outstanding_requested_gpus=outstanding_hp_requested_gpus,
                spot_backlog_gpus=sum(
                    states[job_id].job.gpu_count
                    for job_id in queue
                    if states[job_id].job.service_class == "Spot"
                ),
                running_spot_gpus=running_spot_gpus(),
                hp_running_requested_gpus_by_series=hp_running_by_series,
                demand_series_metadata=demand_series_metadata,
                spot_backlog_gpus_by_pool=spot_backlog_by_pool,
                running_spot_gpus_by_pool=running_by_pool,
                idle_gpus_by_pool=dict(free_by_model),
                maximum_spot_queue_wait_seconds_by_pool=maximum_spot_wait_by_pool,
            )
        if rolling_controller is not None and rolling_controller.is_decision_due(now):
            selected_policy = rolling_controller.decide(
                now=now,
                scheduler_snapshot=build_rolling_snapshot(),
                predictive_observation=latest_predictive_observation,
            )
            if not isinstance(selected_policy, SimulationPolicy):
                raise TypeError("Rolling controller must return a SimulationPolicy")
            selected_policy.validate()
            active_policy = selected_policy
            queue.sort(key=queue_key)
            if predictive_controller is not None:
                predictive_controller.quota_for_guarantee_seconds(
                    active_policy.spot_guarantee_seconds
                )
        start_jobs()
        if preempt_for_hp():
            start_jobs()

        event_times: list[float] = []
        if arrival_index < len(arrivals):
            event_times.append(arrivals[arrival_index].submit_time_seconds)
        event_times.extend(now + states[job_id].remaining for job_id in running)
        if active_policy.scheduler == "priority_preemptive" and queued_hp_count > 0:
            event_times.extend(
                states[job_id].queued_since + active_policy.hp_preemption_delay_seconds
                for job_id in queue
                if states[job_id].job.service_class == "HP"
                and states[job_id].queued_since + active_policy.hp_preemption_delay_seconds
                > now + EPSILON
            )
            event_times.extend(
                float(states[job_id].run_start)
                + float(states[job_id].run_guarantee_seconds or 0)
                for job_id in running
                if states[job_id].job.service_class == "Spot"
                and states[job_id].run_start is not None
                and float(states[job_id].run_start)
                + float(states[job_id].run_guarantee_seconds or 0)
                > now + EPSILON
            )
        if predictive_controller is not None:
            event_times.append(predictive_controller.next_update_time)
            if (
                getattr(predictive_controller, "control_window_only", False)
                and now < evaluation_end - EPSILON
            ):
                event_times.append(evaluation_end)
            if (
                getattr(
                    predictive_controller,
                    "periodic_guarantee_feedback",
                    False,
                )
            ):
                event_times.extend(
                    float(states[job_id].feedback_checkpoint_time)
                    + float(states[job_id].run_guarantee_seconds or 0)
                    for job_id in running
                    if states[job_id].job.service_class == "Spot"
                    and states[job_id].feedback_checkpoint_time is not None
                    and float(states[job_id].feedback_checkpoint_time)
                    + float(states[job_id].run_guarantee_seconds or 0)
                    > now + EPSILON
                    and float(states[job_id].run_guarantee_seconds or 0) > 0
                )
        if rolling_controller is not None and rolling_controller.next_decision_time < float("inf"):
            event_times.append(rolling_controller.next_decision_time)
        future = [value for value in event_times if value > now + EPSILON]
        if not future:
            pending = sorted(set(queue) | running)
            raise RuntimeError(f"Simulation cannot make progress; pending jobs={pending}")
        next_time = min(future)
        delta = next_time - now
        allocated = trace.capacity_gpus - total_free
        allocated_gpu_seconds += allocated * delta
        warmup_overlap = max(
            0.0,
            min(next_time, evaluation_start) - now,
        )
        warmup_allocated_gpu_seconds += allocated * warmup_overlap
        evaluation_overlap = max(
            0.0,
            min(next_time, evaluation_end) - max(now, evaluation_start),
        )
        evaluation_allocated_gpu_seconds += allocated * evaluation_overlap
        for job_id in running:
            states[job_id].remaining = max(0.0, states[job_id].remaining - delta)
        now = next_time

        if (
            predictive_controller is not None
            and getattr(
                predictive_controller,
                "periodic_guarantee_feedback",
                False,
            )
        ):
            for job_id in sorted(running):
                state = states[job_id]
                if (
                    state.job.service_class != "Spot"
                    or state.feedback_checkpoint_time is None
                ):
                    continue
                while (
                    state.feedback_checkpoint_time
                    + float(state.run_guarantee_seconds or 0)
                    <= now + EPSILON
                    and float(state.run_guarantee_seconds or 0) > 0
                ):
                    state.feedback_checkpoint_time += float(
                        state.run_guarantee_seconds or 0
                    )
                    record_controller_feedback(
                        state,
                        state.feedback_checkpoint_time,
                        evicted=False,
                        event_kind="guarantee_duration_completed",
                    )

        ending = sorted(
            [job_id for job_id in running if states[job_id].remaining <= EPSILON]
        )
        for job_id in ending:
            state = states[job_id]
            close_spot_run(state, now, "completed")
            release(state.allocation)
            state.allocation = None
            state.run_start = None
            state.feedback_checkpoint_time = None
            state.run_guarantee_seconds = None
            state.completion = now
            if state.job.service_class == "HP":
                outstanding_hp_requested_gpus = max(
                    0.0, outstanding_hp_requested_gpus - state.job.gpu_count
                )
            running.remove(job_id)
            completed.add(job_id)

    simulation_end = now
    result_policy = (
        rolling_controller.policy_descriptor()
        if rolling_controller is not None
        else policy.to_dict()
    )
    result_policy_fingerprint = (
        rolling_controller.fingerprint
        if rolling_controller is not None
        else policy.fingerprint
    )
    engine: dict[str, Any] = {"name": "schednav-sim", "version": "1"}
    if initial_queue_wait:
        engine["initial_queue_wait"] = {
            "job_count": len(initial_queue_wait),
            "maximum_seconds": max(initial_queue_wait.values()),
            "state_fingerprint": canonical_sha256(initial_queue_wait),
        }
    result: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "engine": engine,
        "trace": {
            "trace_id": trace.trace_id,
            "trace_fingerprint": trace.fingerprint,
            "time_origin": trace.time_origin,
            "source": trace.source,
        },
        "policy": result_policy,
        "policy_fingerprint": result_policy_fingerprint,
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
            "warmup_allocated_gpu_seconds": warmup_allocated_gpu_seconds,
            "evaluation_allocated_gpu_seconds": evaluation_allocated_gpu_seconds,
            "drain_allocated_gpu_seconds": (
                allocated_gpu_seconds
                - warmup_allocated_gpu_seconds
                - evaluation_allocated_gpu_seconds
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
                **(
                    {"tenant_id": state.job.tenant_id}
                    if state.job.tenant_id is not None
                    else {}
                ),
                "queue_seconds": state.queue_seconds,
                "jct_seconds": float(state.completion) - state.job.submit_time_seconds,
                "preemption_count": state.preemptions,
                "run_count": state.run_count,
                "evaluation_population": (
                    evaluation_start
                    <= state.job.submit_time_seconds
                    <= evaluation_end
                ),
            }
            for state in sorted(states.values(), key=lambda item: item.job.job_id)
        ],
        "preemption_events": preemption_events,
        "spot_runs": spot_runs,
    }
    if predictive_controller is not None:
        result["predictive_control"] = predictive_controller.finalize()
    if rolling_controller is not None:
        result["rolling_control"] = rolling_controller.finalize()
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
    evaluation_jobs = [
        job for job in result["jobs"] if bool(job.get("evaluation_population"))
    ]
    for service_class in ("HP", "Spot"):
        selected = [
            job for job in evaluation_jobs if job["service_class"] == service_class
        ]
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
    evaluation_spot_ids = {
        job["job_id"] for job in evaluation_jobs if job["service_class"] == "Spot"
    }
    events = [
        event
        for event in result["preemption_events"]
        if event["preempted_job_id"] in evaluation_spot_ids
    ]
    spot_runs = [
        run for run in result["spot_runs"] if run["job_id"] in evaluation_spot_ids
    ]
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
            == sum(job["run_count"] for job in evaluation_jobs if job["service_class"] == "Spot"),
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
            "spot_eviction_rate_per_run": "Preemption events for evaluation-window Spot arrivals divided by their explicit run starts, including drain.",
            "spot_guarantee_success_rate": "Runs of evaluation-window Spot arrivals completing or remaining uninterrupted for the configured guarantee duration, divided by all their runs, including drain.",
        },
    }
    if "predictive_control" in result:
        control = result["predictive_control"]
        report["predictive_control"] = {
            "available": True,
            "controller_id": control["controller_id"],
            "controller_fingerprint": control["controller_fingerprint"],
            "control_fingerprint": control["control_fingerprint"],
            "information_boundary": control["information_boundary"],
            "update_count": control["update_count"],
            "total_runtime_update_count": control["total_runtime_update_count"],
            "evidence_window_seconds": control["evidence_window_seconds"],
            "eta": control["eta"],
            "spot_quota_gpus": control["spot_quota_gpus"],
            "forecast_evaluation": control["forecast_evaluation"],
        }
        if "admission_diagnostics" in control:
            report["predictive_control"]["admission_diagnostics"] = control[
                "admission_diagnostics"
            ]
    if "rolling_control" in result:
        control = result["rolling_control"]
        report["rolling_control"] = {
            "available": True,
            "controller_id": control["controller_id"],
            "controller_fingerprint": control["controller_fingerprint"],
            "control_fingerprint": control["control_fingerprint"],
            "mode": control["mode"],
            "model_id": control["model_id"],
            "evidence_window_seconds": control["evidence_window_seconds"],
            "decision_count": control["decision_count"],
            "expected_decision_count": control["expected_decision_count"],
            "candidate_budget_per_decision": control[
                "candidate_budget_per_decision"
            ],
            "baseline_action_id": control.get("baseline_action_id", "native-fifo"),
            "candidate_simulation_count": control["candidate_simulation_count"],
            "llm_usage": control["llm_usage"],
            "information_boundary": control["information_boundary"],
            "state_handoff": control["state_handoff"],
            "selected_action_sequence": control["selected_action_sequence"],
        }
    report["metrics_fingerprint"] = canonical_sha256(report)
    return report


def run_native_simulation(trace_path: Path, policy_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    result = simulate_trace(load_canonical_trace(trace_path), SimulationPolicy.load(policy_path))
    return result, build_metrics_report(result)


def run_predictive_simulation(
    trace_path: Path,
    policy_path: Path,
    controller_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    trace = load_canonical_trace(trace_path)
    policy = SimulationPolicy.load(policy_path)
    controller_config = load_controller_config(controller_path)
    controller = create_predictive_controller(
        controller_config,
        trace,
        min(job.submit_time_seconds for job in trace.jobs),
        evidence_start_seconds=(
            trace.evaluation_start_seconds
            if trace.evaluation_start_seconds is not None
            else min(job.submit_time_seconds for job in trace.jobs)
        ),
        evidence_end_seconds=(
            trace.evaluation_end_seconds
            if trace.evaluation_end_seconds is not None
            else max(job.submit_time_seconds for job in trace.jobs)
        ),
    )
    result = simulate_trace(trace, policy, controller)
    return result, build_metrics_report(result)
