"""Cutoff-safe outer rolling policy decisions over the first-party simulator.

The forecasting controller remains the inner Spot-quota loop.  This module
changes only a bounded, registered high-level scheduling policy at explicit
cutoffs.  Candidate simulations use one common scenario built from completed
history and the current agent-safe scheduler snapshot; the real trace after the
cutoff is resumed only after the decision has been frozen.
"""

from __future__ import annotations

from dataclasses import asdict
import math
from statistics import median, pstdev
from typing import Any, Protocol

from .contracts import canonical_sha256
from .metric_catalog import get_metric_value
from .native_simulator import SimulationPolicy, build_metrics_report, simulate_trace
from .native_trace import CanonicalTrace, TraceJob
from .slo import audit_slo_reports


ROLLING_POLICY_SCHEMA = "schednav.rolling-policy/v1"
ROLLING_REPORT_SCHEMA = "schednav.rolling-control-report/v1"
ROLLING_PLAN_SCHEMA = "schednav.rolling-agent-plan/v1"
SCHEDULER_SNAPSHOT_SCHEMA = "schednav.scheduler-state-snapshot/v1"
EPSILON = 1e-9
BASELINE_ACTION_ID = "native-fifo"
MAX_FORECAST_HP_JOBS_PER_POINT = 256
ROLLING_OBSERVATION_SCHEMA = "schednav.rolling-observation/v2"
DEFAULT_SCENARIO_SET_ID = "single-calibrated-p90"
ROBUST_SCENARIO_SET_ID = "dual-forecast-replay-v1"
ALLOWED_SCENARIO_SET_IDS = {
    DEFAULT_SCENARIO_SET_ID,
    ROBUST_SCENARIO_SET_ID,
}


class _ScenarioQuotaController:
    """Frozen cutoff quota used only inside a candidate scenario."""

    control_window_only = True
    resource_pool_scoped = False
    periodic_guarantee_feedback = False

    def __init__(self, projection: dict[str, Any]) -> None:
        self.controller_id = "rolling-frozen-quota-v1"
        self._projection = projection
        self._quotas = {
            int(key): int(value)
            for key, value in projection.get(
                "spot_quota_gpus_by_guarantee_hour", {}
            ).items()
        }
        self._start: float | None = None
        self._end: float | None = None
        self.next_update_time = math.inf
        self.fingerprint = canonical_sha256(
            {
                "controller_id": self.controller_id,
                "projection_fingerprint": projection.get(
                    "projection_fingerprint"
                ),
                "quotas": self._quotas,
            }
        )

    def bind_evidence_window(self, start_seconds: float, end_seconds: float) -> None:
        self._start = float(start_seconds)
        self._end = float(end_seconds)

    def is_update_due(self, now: float) -> bool:
        return False

    def quota_for_guarantee_seconds(
        self, guarantee_seconds: int, resource_pool: str | None = None
    ) -> int:
        required = max(1, int(math.ceil(guarantee_seconds / 3600)))
        horizon = next((item for item in sorted(self._quotas) if item >= required), None)
        if horizon is None:
            raise ValueError("Candidate guarantee exceeds the frozen forecast horizons")
        return self._quotas[horizon]

    def allows_spot(
        self,
        requested_gpus: float,
        running_spot_gpus: float,
        guarantee_seconds: int,
        *,
        resource_pool: str | None = None,
    ) -> bool:
        return (
            running_spot_gpus + requested_gpus
            <= self.quota_for_guarantee_seconds(guarantee_seconds) + EPSILON
        )

    def observe_spot_run_end(self, now: float, **_: Any) -> None:
        return

    def finalize(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": "schednav.predictive-control-report/v1",
            "controller_id": self.controller_id,
            "controller_fingerprint": self.fingerprint,
            "information_boundary": {
                "actual_future_demand_used_for_prediction": False,
                "frozen_cutoff_projection_only": True,
            },
            "update_count": 0,
            "total_runtime_update_count": 0,
            "evidence_window_seconds": {"start": self._start, "end": self._end},
            "eta": {"initial": None, "minimum_observed": None, "maximum_observed": None},
            "spot_quota_gpus": {
                "minimum": min(self._quotas.values()) if self._quotas else 0,
                "maximum": max(self._quotas.values()) if self._quotas else 0,
                "mean": (
                    sum(self._quotas.values()) / len(self._quotas)
                    if self._quotas
                    else 0.0
                ),
            },
            "forecast_evaluation": {
                "scored_point_count": 0,
                "definition": "Not scored inside a counterfactual candidate fork.",
            },
        }
        value["control_fingerprint"] = canonical_sha256(value)
        return value


class CandidateProvider(Protocol):
    """Produce one bounded candidate set without seeing candidate outcomes."""

    provider_id: str
    model_id: str | None

    def select_candidates(
        self,
        observation: dict[str, Any],
        action_catalog: list[dict[str, Any]],
        candidate_budget: int,
    ) -> dict[str, Any]: ...


class RollingDecisionRequired(RuntimeError):
    """Raised by an incremental AgentTeams plan at the next unseen cutoff."""

    def __init__(self, observation: dict[str, Any]) -> None:
        super().__init__(
            f"Rolling candidates required for {observation['observation_fingerprint']}"
        )
        self.observation = observation


def _verified(value: dict[str, Any], fingerprint_field: str) -> bool:
    supplied = value.get(fingerprint_field)
    payload = {key: item for key, item in value.items() if key != fingerprint_field}
    return isinstance(supplied, str) and canonical_sha256(payload) == supplied


def _policy_payload(policy: SimulationPolicy) -> dict[str, Any]:
    return {
        "action_id": policy.action_id,
        "scheduler": policy.scheduler,
        "spot_guarantee_seconds": policy.spot_guarantee_seconds,
        "checkpoint_interval_seconds": policy.checkpoint_interval_seconds,
        "hp_preemption_delay_seconds": policy.hp_preemption_delay_seconds,
        "spot_eviction_budget_rate": policy.spot_eviction_budget_rate,
        "preemption_victim_strategy": policy.preemption_victim_strategy,
        "predictive_admission_mode": policy.predictive_admission_mode,
    }


class WorkloadRuleCandidateProvider:
    """Transparent three-candidate baseline using only the cutoff observation."""

    provider_id = "rolling-workload-rule-v1"
    model_id = None

    def __init__(self, baseline_action_id: str = BASELINE_ACTION_ID) -> None:
        self.baseline_action_id = baseline_action_id

    def select_candidates(
        self,
        observation: dict[str, Any],
        action_catalog: list[dict[str, Any]],
        candidate_budget: int,
    ) -> dict[str, Any]:
        if candidate_budget != 3:
            raise ValueError("rolling-workload-rule-v1 requires candidate_budget=3")
        available = {item["action_id"] for item in action_catalog}

        def first_matching(**expected: Any) -> str | None:
            return next(
                (
                    str(item["action_id"])
                    for item in action_catalog
                    if all(item.get(key) == value for key, value in expected.items())
                ),
                None,
            )

        fifo_guarded = first_matching(
            scheduler="fifo", predictive_admission_mode="enforce"
        )
        preemptive_guarded = first_matching(
            scheduler="priority_preemptive",
            predictive_admission_mode="enforce",
            hp_preemption_delay_seconds=0,
        )
        preemptive_open = first_matching(
            scheduler="priority_preemptive",
            predictive_admission_mode="bypass",
            hp_preemption_delay_seconds=0,
            preemption_victim_strategy="longest_remaining",
        )
        delayed_open = next(
            (
                str(item["action_id"])
                for item in action_catalog
                if item.get("scheduler") == "priority_preemptive"
                and item.get("predictive_admission_mode") == "bypass"
                and int(item.get("hp_preemption_delay_seconds", 0)) > 0
            ),
            None,
        )
        loss_aware_open = first_matching(
            scheduler="priority_preemptive",
            predictive_admission_mode="bypass",
            hp_preemption_delay_seconds=0,
            preemption_victim_strategy="lowest_checkpoint_loss",
        )
        signals = observation["workload_signals"]
        queued_hp = int(observation["scheduler_state"]["queue"]["hp_job_count"])
        hp_pressure = float(signals["hp_peak_active_pressure"])
        spot_share = float(signals["spot_requested_gpu_share"])
        trend = float(signals["hp_recent_to_prior_requested_gpu_ratio"])
        regime = observation.get("multi_timescale_state", {}).get("regime", {})
        risk_level = str(regime.get("risk_level", "unknown"))
        if (
            risk_level == "high"
            or queued_hp > 0
            or hp_pressure >= 0.85
            or trend >= 1.25
        ):
            reason = "high-hp-or-forecast-risk"
            desired = [
                self.baseline_action_id,
                preemptive_open,
                loss_aware_open or preemptive_guarded,
            ]
        elif spot_share >= 0.55:
            reason = "spot-heavy-history"
            desired = [
                self.baseline_action_id,
                preemptive_open,
                delayed_open or fifo_guarded,
            ]
        else:
            reason = "balanced-or-low-pressure-history"
            desired = [
                self.baseline_action_id,
                fifo_guarded,
                preemptive_open,
            ]
        selected = []
        for action_id in [
            *desired,
            fifo_guarded,
            preemptive_guarded,
            preemptive_open,
            delayed_open,
            loss_aware_open,
            *sorted(available),
        ]:
            if action_id is not None and action_id in available and action_id not in selected:
                selected.append(action_id)
            if len(selected) == candidate_budget:
                break
        if self.baseline_action_id not in selected or len(selected) != candidate_budget:
            raise ValueError("Workload rule could not form the bounded baseline set")
        if not set(selected).issubset(available):
            raise ValueError("Workload rule selected an action outside the catalog")
        value: dict[str, Any] = {
            "provider_id": self.provider_id,
            "model_id": None,
            "observation_fingerprint": observation["observation_fingerprint"],
            "candidate_action_ids": selected,
            "reason_code": reason,
            "agent_stage_receipts": [],
            "llm_call_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
        value["selection_fingerprint"] = canonical_sha256(value)
        return value


class RecordedAgentCandidateProvider:
    """Consume fingerprint-bound Single-Agent or AgentTeams decisions.

    The raw model conversation stays in the AgentTeams runtime.  The simulator
    accepts only a normalized plan whose entry is bound to the exact observation
    fingerprint presented at a cutoff.
    """

    def __init__(
        self,
        plan: dict[str, Any],
        baseline_action_id: str = BASELINE_ACTION_ID,
    ) -> None:
        if plan.get("schema_version") != ROLLING_PLAN_SCHEMA or not _verified(
            plan, "plan_fingerprint"
        ):
            raise ValueError("Rolling agent plan is invalid")
        if plan.get("mode") not in {
            "single_agent",
            "multi_agent",
            "multi_agent_masked",
        }:
            raise ValueError("Rolling agent plan mode is unsupported")
        if plan.get("model_id") != "deepseek-v4-flash":
            raise ValueError("Rolling agent plans are locked to deepseek-v4-flash")
        declared_baseline = str(
            plan.get("required_candidate_action_id", BASELINE_ACTION_ID)
        )
        if declared_baseline != baseline_action_id:
            raise ValueError("Rolling agent plan belongs to another safety baseline")
        decisions = plan.get("decisions")
        if not isinstance(decisions, list) or not decisions:
            raise ValueError("Rolling agent plan requires at least one decision")
        by_observation: dict[str, dict[str, Any]] = {}
        for item in decisions:
            fingerprint = item.get("observation_fingerprint")
            if not isinstance(fingerprint, str) or fingerprint in by_observation:
                raise ValueError("Plan observation fingerprints must be unique")
            by_observation[fingerprint] = item
        self.plan = plan
        self._by_observation = by_observation
        self.baseline_action_id = baseline_action_id
        self.provider_id = str(plan["controller_id"])
        self.model_id = str(plan["model_id"])

    def select_candidates(
        self,
        observation: dict[str, Any],
        action_catalog: list[dict[str, Any]],
        candidate_budget: int,
    ) -> dict[str, Any]:
        fingerprint = observation["observation_fingerprint"]
        if fingerprint not in self._by_observation:
            raise ValueError(
                f"Agent plan has no decision for observation {fingerprint}"
            )
        source = self._by_observation[fingerprint]
        candidates = [str(value) for value in source.get("candidate_action_ids", [])]
        available = {item["action_id"] for item in action_catalog}
        if len(candidates) != candidate_budget or len(set(candidates)) != len(candidates):
            raise ValueError("Agent decision does not match the frozen candidate budget")
        if self.baseline_action_id not in candidates or not set(candidates).issubset(available):
            raise ValueError("Agent decision must include FIFO and stay inside the catalog")
        stages = source.get("agent_stage_receipts", [])
        expected_roles = (
            {"Scheduling Strategist"}
            if self.plan["mode"] == "single_agent"
            else {"Workload Analyst", "Scheduling Strategist"}
        )
        roles = {item.get("role") for item in stages}
        if not expected_roles.issubset(roles):
            raise ValueError("Agent decision is missing a required role receipt")
        value: dict[str, Any] = {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "observation_fingerprint": fingerprint,
            "candidate_action_ids": candidates,
            "reason_code": str(source.get("reason_code", "agent-bounded-selection")),
            "agent_stage_receipts": stages,
            "llm_call_count": int(source.get("llm_call_count", len(stages))),
            "prompt_tokens": int(source.get("prompt_tokens", 0)),
            "completion_tokens": int(source.get("completion_tokens", 0)),
        }
        value["selection_fingerprint"] = canonical_sha256(value)
        return value


class IncrementalAgentCandidateProvider:
    """Replay accepted AgentTeams choices and stop at the next new observation."""

    def __init__(
        self,
        *,
        controller_id: str,
        mode: str,
        decisions: list[dict[str, Any]],
        source_project_id: str,
        baseline_action_id: str = BASELINE_ACTION_ID,
    ) -> None:
        if mode not in {"single_agent", "multi_agent", "multi_agent_masked"}:
            raise ValueError("Incremental agent mode is unsupported")
        self.provider_id = controller_id
        self.model_id = "deepseek-v4-flash"
        self.mode = mode
        self.source_project_id = source_project_id
        self.baseline_action_id = baseline_action_id
        self.decisions = list(decisions)
        self._by_observation = {
            str(item["observation_fingerprint"]): item for item in decisions
        }
        if len(self._by_observation) != len(decisions):
            raise ValueError("Incremental decisions require unique observations")

    def select_candidates(
        self,
        observation: dict[str, Any],
        action_catalog: list[dict[str, Any]],
        candidate_budget: int,
    ) -> dict[str, Any]:
        fingerprint = observation["observation_fingerprint"]
        if fingerprint not in self._by_observation:
            raise RollingDecisionRequired(observation)
        plan = build_agent_plan(
            controller_id=self.provider_id,
            mode=self.mode,
            decisions=self.decisions,
            source_project_id=self.source_project_id,
            baseline_action_id=self.baseline_action_id,
        )
        return RecordedAgentCandidateProvider(
            plan, self.baseline_action_id
        ).select_candidates(
            observation,
            action_catalog,
            candidate_budget,
        )


def build_agent_plan(
    *,
    controller_id: str,
    mode: str,
    decisions: list[dict[str, Any]],
    source_project_id: str,
    model_id: str = "deepseek-v4-flash",
    baseline_action_id: str = BASELINE_ACTION_ID,
) -> dict[str, Any]:
    """Normalize AgentTeams outputs into a content-addressed rolling plan."""

    if mode not in {"single_agent", "multi_agent", "multi_agent_masked"}:
        raise ValueError("Unsupported rolling Agent mode")
    if model_id != "deepseek-v4-flash":
        raise ValueError("SchedNav rolling agents are locked to deepseek-v4-flash")
    value: dict[str, Any] = {
        "schema_version": ROLLING_PLAN_SCHEMA,
        "controller_id": controller_id,
        "mode": mode,
        "model_id": model_id,
        "source": {
            "framework": "AgentTeams",
            "project_id": source_project_id,
        },
        "decisions": decisions,
        "definition": (
            "Each entry is generated before its candidate simulations, is bound to "
            "one cutoff-safe observation fingerprint, includes the declared safety "
            "baseline, and contains exactly the declared number of registered "
            "high-level actions."
        ),
    }
    if baseline_action_id != BASELINE_ACTION_ID:
        value["required_candidate_action_id"] = baseline_action_id
    value["plan_fingerprint"] = canonical_sha256(value)
    return value


def _predictive_projection(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    snapshot = value.get("snapshot", {})
    forecast = value.get("forecast", {})
    quota = value.get("quota_plan", {})
    points = []
    for point in forecast.get("points", []):
        projected = {
            key: point[key]
            for key in (
                "target_time_seconds",
                "horizon_step",
                "resource_pool",
                "mu_gpus",
                "sigma_gpus",
                "guarantee_quantile_gpus",
                "raw_guarantee_quantile_gpus",
                "quantile_calibration_offset_gpus",
                "quantile_calibration_method",
            )
            if key in point
        }
        points.append(projected)
    projection: dict[str, Any] = {
        "cutoff_time_seconds": forecast.get("cutoff_time_seconds"),
        "snapshot_fingerprint": snapshot.get("snapshot_fingerprint"),
        "forecast_fingerprint": forecast.get("forecast_fingerprint"),
        "quota_plan_fingerprint": quota.get("quota_plan_fingerprint"),
        "history_ready": (
            snapshot.get("history", {}).get("history_ready")
            if isinstance(snapshot.get("history"), dict)
            else snapshot.get("history_ready")
        ),
        "forecast_points": points,
        "spot_quota_gpus_by_guarantee_hour": quota.get(
            "spot_quota_gpus_by_guarantee_hour", {}
        ),
        "eta": quota.get("eta"),
        "information_boundary": {
            "actual_future_demand_used_for_prediction": False,
        },
    }
    projection["projection_fingerprint"] = canonical_sha256(projection)
    return projection


def _workload_signals(
    trace: CanonicalTrace,
    cutoff: float,
    history_seconds: int,
) -> dict[str, Any]:
    start = cutoff - history_seconds
    jobs = [
        job
        for job in trace.jobs
        if start < job.submit_time_seconds <= cutoff + EPSILON
    ]
    requested = {
        service_class: sum(
            job.gpu_count for job in jobs if job.service_class == service_class
        )
        for service_class in ("HP", "Spot")
    }
    total = requested["HP"] + requested["Spot"]
    half = start + history_seconds / 2
    hp_prior = sum(
        job.gpu_count
        for job in jobs
        if job.service_class == "HP" and job.submit_time_seconds <= half
    )
    hp_recent = requested["HP"] - hp_prior
    samples = [start + index * 3600 for index in range(history_seconds // 3600 + 1)]
    hp_active = [
        sum(
            job.gpu_count
            for job in jobs
            if job.service_class == "HP"
            and job.submit_time_seconds <= sample
            and job.submit_time_seconds + job.duration_seconds > sample
        )
        for sample in samples
    ]
    hp_peak_active_gpus = max(hp_active) if hp_active else 0.0
    return {
        "history_window_seconds": {"start": start, "end": cutoff},
        "observed_job_count": len(jobs),
        "hp_job_count": sum(job.service_class == "HP" for job in jobs),
        "spot_job_count": sum(job.service_class == "Spot" for job in jobs),
        "hp_requested_gpus": round(requested["HP"], 6),
        "spot_requested_gpus": round(requested["Spot"], 6),
        "spot_requested_gpu_share": round(requested["Spot"] / total, 9)
        if total
        else 0.0,
        "hp_peak_active_gpus": round(hp_peak_active_gpus, 6),
        "hp_peak_active_pressure": round(
            hp_peak_active_gpus / trace.capacity_gpus, 9
        ),
        "hp_active_volatility_gpus": round(
            pstdev(hp_active) if len(hp_active) > 1 else 0.0, 6
        ),
        "hp_recent_to_prior_requested_gpu_ratio": round(
            hp_recent / hp_prior, 9
        )
        if hp_prior
        else (2.0 if hp_recent else 1.0),
        "maximum_observed_submit_time_seconds": max(
            (job.submit_time_seconds for job in jobs), default=None
        ),
    }


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator > EPSILON:
        return round(numerator / denominator, 9)
    return 2.0 if numerator > EPSILON else 1.0


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _multi_timescale_state(
    trace: CanonicalTrace,
    cutoff: float,
    history_seconds: int,
    scheduler_snapshot: dict[str, Any],
    predictive_projection: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build deterministic slow/fast state views from cutoff-visible facts only."""

    short_seconds = min(3600, history_seconds)
    short = _workload_signals(trace, cutoff, short_seconds)
    previous_short = _workload_signals(
        trace, cutoff - short_seconds, short_seconds
    )
    long = _workload_signals(trace, cutoff, history_seconds)
    capacity = float(scheduler_snapshot["cluster"]["capacity_gpus"])
    free = float(scheduler_snapshot["cluster"]["free_gpus"])
    running_spot = float(scheduler_snapshot["running"]["spot_gpus"])
    running_hp = max(0.0, capacity - free - running_spot)
    points = (
        predictive_projection.get("forecast_points", [])
        if predictive_projection is not None
        else []
    )
    guarantee = [float(item.get("guarantee_quantile_gpus", 0.0)) for item in points]
    raw_guarantee = [
        float(item.get("raw_guarantee_quantile_gpus", value))
        for item, value in zip(points, guarantee)
    ]
    sigma = [float(item.get("sigma_gpus", 0.0)) for item in points]
    calibration = [
        float(item.get("quantile_calibration_offset_gpus", 0.0)) for item in points
    ]
    quota_values = [
        float(value)
        for value in (
            predictive_projection.get("spot_quota_gpus_by_guarantee_hour", {}).values()
            if predictive_projection is not None
            else []
        )
    ]
    queued_hp = int(scheduler_snapshot["queue"]["hp_job_count"])
    peak_forecast_pressure = max(guarantee, default=0.0) / capacity
    peak_raw_pressure = max(raw_guarantee, default=0.0) / capacity
    short_hp_growth = _safe_ratio(
        float(short["hp_requested_gpus"]),
        float(previous_short["hp_requested_gpus"]),
    )
    short_total = float(short["hp_requested_gpus"]) + float(
        short["spot_requested_gpus"]
    )
    previous_total = float(previous_short["hp_requested_gpus"]) + float(
        previous_short["spot_requested_gpus"]
    )
    high_risk = (
        queued_hp > 0
        or running_hp / capacity >= 0.8
        or peak_forecast_pressure >= 0.9
        or peak_raw_pressure >= 1.0
        or short_hp_growth >= 1.5
    )
    moderate_risk = (
        running_hp / capacity >= 0.6
        or peak_forecast_pressure >= 0.75
        or peak_raw_pressure >= 0.9
        or short_hp_growth >= 1.2
    )
    if high_risk:
        risk_level = "high"
    elif moderate_risk:
        risk_level = "moderate"
    else:
        risk_level = "low"
    if queued_hp > 0 or running_hp / capacity >= 0.8:
        workload_regime = "hp-constrained"
    elif peak_raw_pressure >= 1.0 or peak_forecast_pressure >= 0.9:
        workload_regime = "forecast-constrained"
    elif float(short["spot_requested_gpu_share"]) >= 0.55:
        workload_regime = "spot-heavy"
    else:
        workload_regime = "balanced"
    value: dict[str, Any] = {
        "short_window": short,
        "previous_short_window": previous_short,
        "long_window": long,
        "change": {
            "hp_requested_gpu_ratio_short_vs_previous": short_hp_growth,
            "total_requested_gpu_ratio_short_vs_previous": _safe_ratio(
                short_total, previous_total
            ),
            "spot_share_delta_short_vs_long": round(
                float(short["spot_requested_gpu_share"])
                - float(long["spot_requested_gpu_share"]),
                9,
            ),
        },
        "runtime_pressure": {
            "capacity_gpus": capacity,
            "free_gpus": free,
            "free_gpu_rate": round(free / capacity, 9),
            "running_hp_gpus": round(running_hp, 6),
            "running_hp_pressure": round(running_hp / capacity, 9),
            "running_spot_gpus": round(running_spot, 6),
            "running_spot_pressure": round(running_spot / capacity, 9),
            "queued_hp_job_count": queued_hp,
            "queued_requested_gpus": float(
                scheduler_snapshot["queue"]["requested_gpus"]
            ),
        },
        "forecast_risk": {
            "history_ready": (
                predictive_projection.get("history_ready")
                if predictive_projection is not None
                else None
            ),
            "forecast_point_count": len(points),
            "peak_calibrated_p90_pressure": round(peak_forecast_pressure, 9),
            "peak_raw_p90_pressure": round(peak_raw_pressure, 9),
            "mean_sigma_pressure": round(
                (sum(sigma) / len(sigma) / capacity) if sigma else 0.0, 9
            ),
            "calibration_offset_gpus_min": min(calibration, default=0.0),
            "calibration_offset_gpus_max": max(calibration, default=0.0),
            "minimum_spot_quota_gpus": min(quota_values, default=None),
            "zero_quota_horizon_count": sum(value <= EPSILON for value in quota_values),
        },
        "regime": {
            "workload_regime": workload_regime,
            "risk_level": risk_level,
            "classifier": "deterministic-cutoff-state/v1",
        },
        "information_boundary": {
            "future_arrivals_visible": False,
            "future_job_durations_visible": False,
            "remaining_runtime_visible_to_agent": False,
        },
    }
    value["state_fingerprint"] = canonical_sha256(value)
    return value


def _scenario_projection_variants(
    projection: dict[str, Any] | None,
    scenario_set_id: str,
) -> list[tuple[str, dict[str, Any] | None]]:
    """Return cutoff-safe forecast variants for deterministic candidate forks."""

    if scenario_set_id == DEFAULT_SCENARIO_SET_ID:
        return [("calibrated-p90", projection)]
    if scenario_set_id != ROBUST_SCENARIO_SET_ID:
        raise ValueError("Unsupported rolling scenario set")
    history_replay = {
        **(projection or {}),
        "base_projection_fingerprint": (
            projection.get("projection_fingerprint")
            if projection is not None
            else None
        ),
        "scenario_profile_id": "recent-history-replay",
        "forecast_points": [],
    }
    history_replay.pop("projection_fingerprint", None)
    history_replay["projection_fingerprint"] = canonical_sha256(history_replay)
    return [
        ("calibrated-p90", projection),
        ("recent-history-replay", history_replay),
    ]


def _decompose_forecast_hp_request(
    requested_gpus: float,
    templates: list[dict[str, Any]],
    *,
    capacity_gpus: float,
    point_index: int,
) -> list[dict[str, Any]]:
    """Represent one aggregate HP forecast with bounded, observed job shapes.

    The predictor estimates aggregate concurrent demand.  Treating that value as
    one gang job changes its scheduling semantics, so candidate scenarios split
    the aggregate into deterministic pieces shaped by jobs visible at the
    cutoff.  A bounded fallback keeps sparse/fractional traces tractable without
    recreating a cluster-sized synthetic job.
    """

    requested = min(float(capacity_gpus), max(0.0, float(requested_gpus)))
    if requested <= EPSILON:
        return []
    usable = [
        item
        for item in templates
        if EPSILON < float(item.get("gpu_count", 0.0)) <= capacity_gpus + EPSILON
    ]
    if usable:
        offset = point_index % len(usable)
        ordered = usable[offset:] + usable[:offset]
    else:
        ordered = [
            {
                "gpu_count": min(requested, max(1.0, capacity_gpus / 64.0)),
                "tenant_id": "forecast-aggregate",
                "source_job_id": None,
            }
        ]
    minimum_piece = requested / MAX_FORECAST_HP_JOBS_PER_POINT
    remaining = requested
    pieces: list[dict[str, Any]] = []
    while remaining > EPSILON and len(pieces) < MAX_FORECAST_HP_JOBS_PER_POINT:
        template = ordered[len(pieces) % len(ordered)]
        template_size = max(minimum_piece, float(template["gpu_count"]))
        gpu_count = min(remaining, template_size)
        pieces.append(
            {
                "gpu_count": gpu_count,
                "tenant_id": template.get("tenant_id") or "forecast-aggregate",
                "source_job_id": template.get("source_job_id"),
            }
        )
        remaining -= gpu_count
    if remaining > EPSILON:
        # The minimum-piece bound should make this unreachable; retain an exact,
        # deterministic guard so the aggregate demand is never silently lost.
        pieces[-1]["gpu_count"] = float(pieces[-1]["gpu_count"]) + remaining
    return pieces


def _scenario_trace(
    trace: CanonicalTrace,
    workload_trace: CanonicalTrace,
    cutoff: float,
    horizon_seconds: int,
    scheduler_snapshot: dict[str, Any],
    predictive_projection: dict[str, Any] | None,
    *,
    use_actual_future: bool,
) -> CanonicalTrace:
    history_start = cutoff - horizon_seconds
    completed_history = [
        job
        for job in workload_trace.jobs
        if history_start < job.submit_time_seconds <= cutoff + EPSILON
        and job.submit_time_seconds + job.duration_seconds <= cutoff + EPSILON
    ]
    visible_completed: dict[tuple[str, str], TraceJob] = {}
    for source_trace in (trace, workload_trace):
        for job in source_trace.jobs:
            if (
                job.submit_time_seconds <= cutoff + EPSILON
                and job.submit_time_seconds + job.duration_seconds
                <= cutoff + EPSILON
            ):
                visible_completed[(job.job_id, job.service_class)] = job
    duration_samples: dict[tuple[str, str], list[float]] = {}
    duration_samples_by_class: dict[str, list[float]] = {
        "HP": [],
        "Spot": [],
    }
    for job in visible_completed.values():
        duration_samples.setdefault(
            (job.service_class, job.gpu_model), []
        ).append(float(job.duration_seconds))
        duration_samples_by_class[job.service_class].append(
            float(job.duration_seconds)
        )

    def estimated_remaining(item: dict[str, Any]) -> tuple[float, str, int]:
        service_class = str(item["service_class"])
        elapsed = (
            float(item.get("current_run_elapsed_seconds", 0.0))
            if item.get("status") == "running"
            else 0.0
        )
        samples = duration_samples.get(
            (service_class, str(item["gpu_model"])), []
        ) or duration_samples_by_class[service_class]
        survivors = [duration - elapsed for duration in samples if duration > elapsed]
        if survivors:
            estimate = float(median(survivors))
            method = "conditional-survival-median"
            sample_count = len(survivors)
        elif samples:
            estimate = max(3600.0, float(median(samples)))
            method = "right-censored-class-median-fallback"
            sample_count = len(samples)
        else:
            estimate = 3600.0
            method = "sparse-history-one-hour-fallback"
            sample_count = 0
        return min(float(horizon_seconds), max(60.0, estimate)), method, sample_count

    jobs: list[TraceJob] = []
    initial_queue_wait_seconds: dict[str, float] = {}
    carryover_estimates: list[dict[str, Any]] = []
    hp_templates_by_pool: dict[str, list[dict[str, Any]]] = {}
    for job in completed_history:
        if job.service_class != "HP":
            continue
        hp_templates_by_pool.setdefault(job.gpu_model, []).append(
            {
                "gpu_count": job.gpu_count,
                "tenant_id": job.tenant_id,
                "source_job_id": job.job_id,
            }
        )
    for index, item in enumerate(scheduler_snapshot["carryover_jobs"]):
        duration, estimator_method, estimator_sample_count = estimated_remaining(item)
        scenario_job_id = f"carryover::{index:06d}::{item['job_id']}"
        jobs.append(
            TraceJob(
                job_id=scenario_job_id,
                submit_time_seconds=cutoff,
                duration_seconds=duration,
                gpu_count=float(item["gpu_count"]),
                service_class=str(item["service_class"]),
                gpu_model=str(item["gpu_model"]),
                tenant_id=item.get("tenant_id"),
            )
        )
        queue_wait = float(item.get("current_queue_wait_seconds", 0.0))
        if item.get("status") == "queued" and queue_wait > EPSILON:
            initial_queue_wait_seconds[scenario_job_id] = queue_wait
        carryover_estimates.append(
            {
                "scenario_job_id": scenario_job_id,
                "service_class": item["service_class"],
                "gpu_model": item["gpu_model"],
                "gpu_count": float(item["gpu_count"]),
                "status": item.get("status"),
                "current_run_elapsed_seconds": float(
                    item.get("current_run_elapsed_seconds", 0.0)
                ),
                "current_queue_wait_seconds": queue_wait,
                "estimated_remaining_seconds": duration,
                "estimator_method": estimator_method,
                "estimator_sample_count": estimator_sample_count,
            }
        )
        if item["service_class"] == "HP":
            hp_templates_by_pool.setdefault(str(item["gpu_model"]), []).append(
                {
                    "gpu_count": float(item["gpu_count"]),
                    "tenant_id": item.get("tenant_id"),
                    "source_job_id": item["job_id"],
                }
            )
    forecast_points = (
        predictive_projection.get("forecast_points", [])
        if predictive_projection is not None
        else []
    )
    if use_actual_future:
        representative = [
            job
            for job in trace.jobs
            if cutoff < job.submit_time_seconds <= cutoff + horizon_seconds
        ]
        for index, job in enumerate(representative):
            jobs.append(
                TraceJob(
                    job_id=f"future::{index:06d}::{job.job_id}",
                    submit_time_seconds=job.submit_time_seconds,
                    duration_seconds=job.duration_seconds,
                    gpu_count=job.gpu_count,
                    service_class=job.service_class,
                    gpu_model=job.gpu_model,
                    tenant_id=job.tenant_id,
                )
            )
    else:
        for index, job in enumerate(completed_history):
            if forecast_points and job.service_class == "HP":
                continue
            jobs.append(
                TraceJob(
                    job_id=f"history::{index:06d}::{job.job_id}",
                    submit_time_seconds=job.submit_time_seconds + horizon_seconds,
                    duration_seconds=job.duration_seconds,
                    gpu_count=job.gpu_count,
                    service_class=job.service_class,
                    gpu_model=job.gpu_model,
                    tenant_id=job.tenant_id,
                )
            )
        capacities: dict[str, float] = {}
        for node in trace.nodes:
            capacities[node.gpu_model] = capacities.get(node.gpu_model, 0.0) + node.gpu_count
        decomposition_points: list[dict[str, Any]] = []
        for index, point in enumerate(forecast_points):
            target = float(point["target_time_seconds"])
            if target <= cutoff + EPSILON or target > cutoff + horizon_seconds + EPSILON:
                continue
            resource_pool = str(point.get("resource_pool", "*"))
            if resource_pool == "*":
                resource_pool = next(iter(capacities))
                capacity = trace.capacity_gpus
            elif resource_pool in capacities:
                capacity = capacities[resource_pool]
            else:
                continue
            forecast_total = min(
                capacity,
                max(0.0, float(point.get("guarantee_quantile_gpus", 0.0))),
            )
            target_offset = target - cutoff
            surviving_carryover_hp = sum(
                float(item["gpu_count"])
                for item in carryover_estimates
                if item["service_class"] == "HP"
                and item["status"] == "running"
                and (
                    resource_pool == "*"
                    or str(item["gpu_model"]) in {resource_pool, "*"}
                )
                and float(item["estimated_remaining_seconds"])
                > target_offset + EPSILON
            )
            requested = max(0.0, forecast_total - surviving_carryover_hp)
            if requested <= EPSILON:
                continue
            duration = min(3600.0, cutoff + horizon_seconds - (target - 3600.0))
            templates = list(hp_templates_by_pool.get(resource_pool, []))
            templates.extend(hp_templates_by_pool.get("*", []))
            pieces = _decompose_forecast_hp_request(
                requested,
                templates,
                capacity_gpus=capacity,
                point_index=index,
            )
            for piece_index, piece in enumerate(pieces):
                jobs.append(
                    TraceJob(
                        job_id=(
                            f"forecast-hp::{index:06d}::{piece_index:04d}::"
                            f"{resource_pool}"
                        ),
                        submit_time_seconds=max(cutoff, target - 3600.0),
                        duration_seconds=max(60.0, duration),
                        gpu_count=float(piece["gpu_count"]),
                        service_class="HP",
                        gpu_model=resource_pool,
                        tenant_id=str(piece["tenant_id"]),
                    )
                )
            decomposition_points.append(
                {
                    "target_time_seconds": target,
                    "resource_pool": resource_pool,
                    "forecast_quantile_gpus": round(
                        float(point.get("guarantee_quantile_gpus", 0.0)), 6
                    ),
                    "forecast_total_active_gpus": round(forecast_total, 6),
                    "surviving_carryover_hp_gpus": round(
                        surviving_carryover_hp, 6
                    ),
                    "scenario_incremental_hp_gpus": round(requested, 6),
                    "scenario_target_gpus": round(requested, 6),
                    "visible_template_count": len(templates),
                    "synthetic_job_count": len(pieces),
                    "maximum_synthetic_job_gpus": round(
                        max(float(piece["gpu_count"]) for piece in pieces), 6
                    ),
                }
            )
    if not jobs:
        raise ValueError("Rolling scenario contains no observable carryover or history")
    jobs.sort(key=lambda item: (item.submit_time_seconds, item.job_id))
    source = {
        "dataset": "rolling-decision-scenario",
        "generator": "schednav.past-replay-scenario/v3",
        "cutoff_time_seconds": cutoff,
        "horizon_seconds": horizon_seconds,
        "scheduler_snapshot_fingerprint": scheduler_snapshot["snapshot_fingerprint"],
        "future_arrivals_visible": use_actual_future,
        "carryover_runtime_estimator": (
            "cutoff-visible-conditional-survival-median/v1"
        ),
        "carryover_runtime_estimates": carryover_estimates,
        "initial_queue_wait_seconds_by_job_id": initial_queue_wait_seconds,
        "forecast_derived_hp_jobs": bool(forecast_points and not use_actual_future),
        "forecast_hp_decomposition": (
            {
                "method": "cutoff-visible-job-shape-after-carryover/v2",
                "aggregate_demand_preserved": True,
                "forecast_is_total_active_demand": True,
                "surviving_carryover_subtracted_before_decomposition": True,
                "future_job_shapes_visible": False,
                "maximum_jobs_per_forecast_point": MAX_FORECAST_HP_JOBS_PER_POINT,
                "points": decomposition_points,
            }
            if forecast_points and not use_actual_future
            else None
        ),
        "forecast_projection_fingerprint": (
            predictive_projection.get("projection_fingerprint")
            if predictive_projection is not None and not use_actual_future
            else None
        ),
    }
    payload = {
        "trace_id": f"{trace.trace_id}-scenario-{int(cutoff)}",
        "source": source,
        "nodes": [asdict(node) for node in trace.nodes],
        "jobs": [asdict(job) for job in jobs],
        "evaluation_start_seconds": cutoff,
        "evaluation_end_seconds": cutoff + horizon_seconds,
    }
    fingerprint = canonical_sha256(payload)
    return CanonicalTrace(
        trace_id=payload["trace_id"],
        time_origin=trace.time_origin,
        source=source,
        nodes=trace.nodes,
        jobs=tuple(jobs),
        fingerprint=fingerprint,
        evaluation_start_seconds=cutoff,
        evaluation_end_seconds=cutoff + horizon_seconds,
        schema_version=trace.schema_version,
    )


def _rank_candidate_summaries(
    candidates: list[dict[str, Any]],
    slo: dict[str, Any],
    active_action_id: str,
    baseline_action_id: str = BASELINE_ACTION_ID,
) -> dict[str, Any]:
    ranking = slo.get("ranking")
    if not isinstance(ranking, dict):
        raise ValueError("SLO spec has no hierarchical ranking")
    remaining = [
        item
        for item in candidates
        if item["hard_slo_passed"]
        and item["allocation_rate_mean"] is not None
    ]
    stages: list[dict[str, Any]] = [
        {
            "stage": "hard_slo_filter",
            "remaining_action_ids": [item["action_id"] for item in remaining],
        }
    ]
    if remaining:
        best = max(item["allocation_rate_mean"] for item in remaining)
        band = float(ranking["allocation_tie_band"])
        remaining = [
            item for item in remaining if best - item["allocation_rate_mean"] < band
        ]
        stages.append(
            {
                "stage": "maximize_allocation_rate",
                "best_observed": best,
                "strict_tie_band": band,
                "remaining_action_ids": [item["action_id"] for item in remaining],
            }
        )
    if len(remaining) > 1:
        observed = [
            item["spot_jct_p95_seconds"]
            for item in remaining
            if item["spot_jct_p95_seconds"] is not None
        ]
        if observed:
            best = min(observed)
            remaining = [
                item for item in remaining if item["spot_jct_p95_seconds"] == best
            ]
            stages.append(
                {
                    "stage": "minimize_spot_p95_jct",
                    "best_observed": best,
                    "remaining_action_ids": [item["action_id"] for item in remaining],
                }
            )
    if len(remaining) > 1:
        observed = [
            item["spot_eviction_rate_per_run"]
            for item in remaining
            if item["spot_eviction_rate_per_run"] is not None
        ]
        if observed:
            best = min(observed)
            remaining = [
                item
                for item in remaining
                if item["spot_eviction_rate_per_run"] == best
            ]
            stages.append(
                {
                    "stage": "minimize_spot_eviction_rate_per_run",
                    "best_observed": best,
                    "remaining_action_ids": [item["action_id"] for item in remaining],
                }
            )
    if len(remaining) > 1 and all(
        item.get("stress_scenario_pass_count") is not None for item in remaining
    ):
        best = max(int(item["stress_scenario_pass_count"]) for item in remaining)
        remaining = [
            item
            for item in remaining
            if int(item["stress_scenario_pass_count"]) == best
        ]
        stages.append(
            {
                "stage": "maximize_stress_scenario_pass_count_after_slo_tie",
                "best_observed": best,
                "remaining_action_ids": [item["action_id"] for item in remaining],
            }
        )
    frontier = [item["action_id"] for item in remaining]
    fallback = None
    if not remaining:
        selected = baseline_action_id
        status = "no_eligible_policy_safe_fifo"
        fallback = "No candidate passed every hard SLO; execute FIFO safety fallback."
    elif len(remaining) == 1:
        selected = remaining[0]["action_id"]
        status = "selected"
    else:
        status = "tie_with_declared_safety_fallback"
        if active_action_id in frontier:
            selected = active_action_id
            fallback = "Retain the current action when the declared hierarchy is tied."
        else:
            active = next(
                (
                    item
                    for item in candidates
                    if item["action_id"] == active_action_id
                    and isinstance(item.get("control_profile"), dict)
                ),
                None,
            )
            controlled_fields = (
                "scheduler",
                "spot_guarantee_seconds",
                "checkpoint_interval_seconds",
                "hp_preemption_delay_seconds",
                "spot_eviction_budget_rate",
                "preemption_victim_strategy",
                "predictive_admission_mode",
            )
            nearest: list[dict[str, Any]] = []
            minimum_distance: int | None = None
            if active is not None:
                active_profile = active["control_profile"]
                distances = [
                    (
                        sum(
                            item.get("control_profile", {}).get(field)
                            != active_profile.get(field)
                            for field in controlled_fields
                        ),
                        item,
                    )
                    for item in remaining
                ]
                minimum_distance = min(distance for distance, _item in distances)
                nearest = [
                    item
                    for distance, item in distances
                    if distance == minimum_distance
                ]
            if len(nearest) == 1:
                selected = nearest[0]["action_id"]
                status = "tie_resolved_by_minimum_control_change"
                fallback = (
                    "Choose the tied eligible action with the fewest changed "
                    "registered controls relative to the active action."
                )
                stages.append(
                    {
                        "stage": "minimize_control_changes",
                        "distance": minimum_distance,
                        "remaining_action_ids": [selected],
                    }
                )
            elif baseline_action_id in frontier:
                selected = baseline_action_id
                fallback = "Use FIFO when it remains on an unresolved tied frontier."
            else:
                selected = sorted(frontier)[0]
                fallback = "Use lexical action ID only as the final declared fallback."
    value: dict[str, Any] = {
        "selection_status": status,
        "candidates": candidates,
        "stages": stages,
        "frontier_action_ids": frontier,
        "selected_action_id": selected,
        "execution_fallback": fallback,
        "weighted_score_used": False,
    }
    value["ranking_fingerprint"] = canonical_sha256(value)
    return value


def _rank_candidates(
    policies: list[SimulationPolicy],
    metrics: list[dict[str, Any]],
    audits: list[dict[str, Any]],
    slo: dict[str, Any],
    active_action_id: str,
    baseline_action_id: str = BASELINE_ACTION_ID,
) -> dict[str, Any]:
    """Rank one-scenario evidence through the common hierarchical selector."""

    ranking = slo.get("ranking")
    if not isinstance(ranking, dict):
        raise ValueError("SLO spec has no hierarchical ranking")
    candidates = []
    for policy, metric, audit in zip(policies, metrics, audits):
        allocation = get_metric_value(metric, ranking["allocation_metric"])
        spot_jct = get_metric_value(metric, ranking["second_metric"])
        eviction = get_metric_value(metric, ranking["third_metric"])
        candidates.append(
            {
                "action_id": policy.action_id,
                "policy_fingerprint": policy.fingerprint,
                "control_profile": _policy_payload(policy),
                "metrics_fingerprint": metric["metrics_fingerprint"],
                "audit_fingerprint": audit["audit_fingerprint"],
                "hard_slo_passed": audit["audit_passed"] is True,
                "allocation_rate_mean": (
                    float(allocation) if allocation is not None else None
                ),
                "spot_jct_p95_seconds": (
                    float(spot_jct) if spot_jct is not None else None
                ),
                "spot_eviction_rate_per_run": (
                    float(eviction) if eviction is not None else None
                ),
            }
        )
    return _rank_candidate_summaries(
        candidates,
        slo,
        active_action_id,
        baseline_action_id,
    )


def _audit_candidate_slo(
    metrics: dict[str, Any],
    slo: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Apply the formal SLO with vacuous zero-population class constraints.

    The final real-window audit remains unchanged.  A cutoff scenario may
    legitimately contain no observed HP or Spot arrivals, so class-specific
    constraints are marked not applicable instead of rejecting every action.
    """

    audit = audit_slo_reports(metrics, slo, baseline)
    prospective_constraint_ids = {
        "hp-completion-rate",
        "hp-preempted-job-count",
        "hp-p95-queue-seconds",
        "spot-completion-rate",
        "spot-eviction-rate-per-run",
        "spot-guarantee-success-rate",
    }
    populations = {
        service_class.lower(): int(metrics["jobs"][service_class]["job_count"])
        for service_class in ("HP", "Spot")
    }
    for item in audit["results"]:
        prefix = str(item["metric"]).split("_", 1)[0]
        if (
            item["status"] == "unavailable"
            and prefix in populations
            and populations[prefix] == 0
        ):
            item["status"] = "not_applicable_zero_population"
            item["passed"] = True
        elif (
            item["severity"] == "hard"
            and item["id"] not in prospective_constraint_ids
        ):
            item["status"] = "final_window_only"
            item["passed"] = True
    hard_results = [item for item in audit["results"] if item["severity"] == "hard"]
    audit["audit_passed"] = (
        audit["metrics_schema_supported"]
        and audit["metrics_fingerprint_valid"]
        and all(
            value
            for key, value in audit["evidence_checks"].items()
            if key != "baseline_required"
        )
        and all(item["passed"] for item in hard_results)
    )
    audit["soft_violation_count"] = sum(
        item["severity"] == "soft" and not item["passed"]
        for item in audit["results"]
    )
    audit["candidate_scope"] = {
        "zero_population_constraints_are_vacuous": True,
        "prospective_constraint_ids": sorted(prospective_constraint_ids),
        "final_window_only_constraints": sorted(
            item["id"]
            for item in audit["results"]
            if item["status"] == "final_window_only"
        ),
        "final_real_window_audit_uses_the_unmodified_formal_slo": True,
    }
    audit.pop("audit_fingerprint", None)
    audit["audit_fingerprint"] = canonical_sha256(audit)
    return audit


class RollingPolicyController:
    """Simulation-backed high-level controller with exact runtime state handoff."""

    def __init__(
        self,
        *,
        controller_id: str,
        mode: str,
        trace: CanonicalTrace,
        policies: list[SimulationPolicy],
        slo: dict[str, Any],
        candidate_provider: CandidateProvider,
        decision_interval_seconds: int = 14400,
        scenario_horizon_seconds: int = 14400,
        history_window_seconds: int = 14400,
        candidate_budget: int = 3,
        use_actual_future: bool = False,
        workload_trace: CanonicalTrace | None = None,
        baseline_action_id: str = BASELINE_ACTION_ID,
        scenario_set_id: str = DEFAULT_SCENARIO_SET_ID,
        minimum_action_hold_seconds: int = 0,
    ) -> None:
        if mode not in {
            "workload_rule",
            "single_agent",
            "multi_agent",
            "multi_agent_masked",
            "catalog_oracle",
        }:
            raise ValueError("Unsupported rolling controller mode")
        if decision_interval_seconds <= 0 or scenario_horizon_seconds <= 0:
            raise ValueError("Rolling intervals must be positive")
        if history_window_seconds < scenario_horizon_seconds:
            raise ValueError("history_window_seconds must cover the scenario horizon")
        if not 3 <= candidate_budget <= 5:
            raise ValueError("candidate_budget must be between 3 and 5")
        by_id = {policy.action_id: policy for policy in policies}
        if len(by_id) != len(policies) or baseline_action_id not in by_id:
            raise ValueError("Rolling catalog requires a unique declared safety baseline")
        if mode == "catalog_oracle" and (not use_actual_future or candidate_budget != len(policies)):
            raise ValueError("Catalog oracle must evaluate the full catalog on actual future")
        if mode != "catalog_oracle" and use_actual_future:
            raise ValueError("Deployable rolling controllers cannot inspect actual future")
        if scenario_set_id not in ALLOWED_SCENARIO_SET_IDS:
            raise ValueError("Unsupported rolling scenario set")
        if minimum_action_hold_seconds < 0:
            raise ValueError("minimum_action_hold_seconds cannot be negative")
        self.controller_id = controller_id
        self.mode = mode
        self.trace = trace
        self.workload_trace = workload_trace or trace
        if (
            self.workload_trace.evaluation_start_seconds
            != trace.evaluation_start_seconds
            or self.workload_trace.evaluation_end_seconds
            != trace.evaluation_end_seconds
            or self.workload_trace.capacity_gpus != trace.capacity_gpus
        ):
            raise ValueError(
                "Workload-history trace must share the execution window and capacity"
            )
        self.policies = by_id
        self.slo = slo
        self.provider = candidate_provider
        self.decision_interval_seconds = decision_interval_seconds
        self.scenario_horizon_seconds = scenario_horizon_seconds
        self.history_window_seconds = history_window_seconds
        self.candidate_budget = candidate_budget
        self.use_actual_future = use_actual_future
        self.baseline_action_id = baseline_action_id
        self.scenario_set_id = scenario_set_id
        self.minimum_action_hold_seconds = minimum_action_hold_seconds
        self._active_action_since: float | None = None
        self._evaluation_start: float | None = None
        self._evaluation_end: float | None = None
        self._next_decision_time = math.inf
        self._decisions: list[dict[str, Any]] = []
        self._active_action_id = baseline_action_id
        self._config = {
            "schema_version": ROLLING_POLICY_SCHEMA,
            "controller_id": controller_id,
            "mode": mode,
            "decision_interval_seconds": decision_interval_seconds,
            "scenario_horizon_seconds": scenario_horizon_seconds,
            "history_window_seconds": history_window_seconds,
            "candidate_budget": candidate_budget,
            "catalog": [_policy_payload(policy) for policy in policies],
            "candidate_provider_id": candidate_provider.provider_id,
            "model_id": candidate_provider.model_id,
            "use_actual_future": use_actual_future,
            "scenario_set_id": scenario_set_id,
            "minimum_action_hold_seconds": minimum_action_hold_seconds,
            "workload_history_trace_fingerprint": self.workload_trace.fingerprint,
            "placement_strategy": "deterministic_best_fit",
        }
        if baseline_action_id != BASELINE_ACTION_ID:
            self._config["baseline_action_id"] = baseline_action_id
        self.fingerprint = canonical_sha256(self._config)

    @property
    def next_decision_time(self) -> float:
        return self._next_decision_time

    def bind_evidence_window(self, start_seconds: float, end_seconds: float) -> None:
        if end_seconds <= start_seconds:
            raise ValueError("Rolling evidence window must have positive duration")
        if self._evaluation_start is not None and (
            self._evaluation_start != start_seconds or self._evaluation_end != end_seconds
        ):
            raise ValueError("Rolling controller cannot be rebound to another window")
        self._evaluation_start = float(start_seconds)
        self._evaluation_end = float(end_seconds)
        self._next_decision_time = float(start_seconds)

    def is_decision_due(self, now: float) -> bool:
        return (
            self._evaluation_end is not None
            and self._next_decision_time < self._evaluation_end - EPSILON
            and now + EPSILON >= self._next_decision_time
        )

    def _observation(
        self,
        now: float,
        scheduler_snapshot: dict[str, Any],
        predictive_observation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if scheduler_snapshot.get("schema_version") != SCHEDULER_SNAPSHOT_SCHEMA or not _verified(
            scheduler_snapshot, "snapshot_fingerprint"
        ):
            raise ValueError("Rolling scheduler snapshot is invalid")
        if scheduler_snapshot["information_boundary"]["future_arrivals_visible"] is not False:
            raise ValueError("Rolling scheduler snapshot exposes future arrivals")
        signals = _workload_signals(
            self.workload_trace, now, self.history_window_seconds
        )
        if (
            signals["maximum_observed_submit_time_seconds"] is not None
            and float(signals["maximum_observed_submit_time_seconds"]) > now + EPSILON
        ):
            raise ValueError("Workload projection crossed the cutoff")
        projection = _predictive_projection(predictive_observation)
        value: dict[str, Any] = {
            "schema_version": ROLLING_OBSERVATION_SCHEMA,
            "controller_id": self.controller_id,
            "cutoff_time_seconds": float(now),
            "scheduler_state": scheduler_snapshot,
            "workload_signals": signals,
            "multi_timescale_state": _multi_timescale_state(
                self.workload_trace,
                now,
                self.history_window_seconds,
                scheduler_snapshot,
                projection,
            ),
            "predictive_projection": projection,
            "candidate_scenario_set_id": self.scenario_set_id,
            "required_candidate_action_id": self.baseline_action_id,
            "action_catalog": [
                _policy_payload(policy) for policy in self.policies.values()
            ],
            "information_boundary": {
                "jobs_with_submit_time_after_cutoff_excluded": True,
                "actual_future_scenario_used": self.use_actual_future,
                "agent_receives_actual_future": False,
                "job_remaining_runtime_exposed_to_agent": False,
            },
        }
        value["observation_fingerprint"] = canonical_sha256(value)
        return value

    def decide(
        self,
        *,
        now: float,
        scheduler_snapshot: dict[str, Any],
        predictive_observation: dict[str, Any] | None,
    ) -> SimulationPolicy:
        if not self.is_decision_due(now) or abs(now - self._next_decision_time) > EPSILON:
            raise ValueError("Rolling decision was requested outside its declared cutoff")
        observation = self._observation(now, scheduler_snapshot, predictive_observation)
        if self.mode == "catalog_oracle":
            candidate_ids = list(self.policies)
            selection: dict[str, Any] = {
                "provider_id": self.provider.provider_id,
                "model_id": None,
                "observation_fingerprint": observation["observation_fingerprint"],
                "candidate_action_ids": candidate_ids,
                "reason_code": "post-hoc-full-catalog-upper-bound",
                "agent_stage_receipts": [],
                "llm_call_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }
            selection["selection_fingerprint"] = canonical_sha256(selection)
        else:
            selection = self.provider.select_candidates(
                observation,
                observation["action_catalog"],
                self.candidate_budget,
            )
            if not _verified(selection, "selection_fingerprint"):
                raise ValueError("Candidate selection fingerprint is invalid")
            candidate_ids = selection["candidate_action_ids"]
        if (
            len(candidate_ids) != self.candidate_budget
            or self.baseline_action_id not in candidate_ids
        ):
            raise ValueError(
                "Every rolling decision must consume its exact budget and include "
                "the declared safety baseline"
            )
        candidate_policies = [self.policies[action_id] for action_id in candidate_ids]
        projection = observation["predictive_projection"]
        projection_variants = (
            [("actual-future", projection)]
            if self.use_actual_future
            else _scenario_projection_variants(projection, self.scenario_set_id)
        )
        scenario_runs: list[dict[str, Any]] = []
        for scenario_profile_id, scenario_projection in projection_variants:
            scenario = _scenario_trace(
                self.trace,
                self.workload_trace,
                now,
                self.scenario_horizon_seconds,
                scheduler_snapshot,
                scenario_projection,
                use_actual_future=self.use_actual_future,
            )
            initial_queue_wait = {
                str(job_id): float(seconds)
                for job_id, seconds in scenario.source.get(
                    "initial_queue_wait_seconds_by_job_id", {}
                ).items()
            }
            candidate_metrics = []
            candidate_results = []
            for candidate in candidate_policies:
                # The workload scenario may be stress-adjusted, but an enforcing
                # action executes the quota actually frozen at the cutoff.
                frozen_quota = (
                    _ScenarioQuotaController(projection)
                    if candidate.predictive_admission_mode == "enforce"
                    and projection is not None
                    and projection.get("spot_quota_gpus_by_guarantee_hour")
                    else None
                )
                candidate_result = simulate_trace(
                    scenario,
                    candidate,
                    frozen_quota,
                    initial_queue_wait_seconds_by_job_id=initial_queue_wait,
                )
                candidate_results.append(candidate_result)
                candidate_metrics.append(build_metrics_report(candidate_result))
            baseline_metrics = next(
                metric
                for policy, metric in zip(candidate_policies, candidate_metrics)
                if policy.action_id == self.baseline_action_id
            )
            audits = [
                _audit_candidate_slo(metric, self.slo, baseline_metrics)
                for metric in candidate_metrics
            ]
            scenario_runs.append(
                {
                    "scenario_profile_id": scenario_profile_id,
                    "scenario": scenario,
                    "results": candidate_results,
                    "metrics": candidate_metrics,
                    "audits": audits,
                }
            )

        candidate_evidence: list[dict[str, Any]] = []
        queue_threshold = float(
            next(
                item["threshold"]
                for item in self.slo["constraints"]
                if item["id"] == "hp-p95-queue-seconds"
            )
        )
        completed_hp_queue = [
            float(value)
            for value in scheduler_snapshot.get("slo_progress", {}).get(
                "hp_completed_queue_seconds", []
            )
        ]
        for candidate_index, policy in enumerate(candidate_policies):
            scenario_evidence = []
            for scenario_run in scenario_runs:
                result = scenario_run["results"][candidate_index]
                metric = scenario_run["metrics"][candidate_index]
                audit = scenario_run["audits"][candidate_index]
                candidate_hp_queue = [
                    float(job["queue_seconds"])
                    for job in result["jobs"]
                    if job["service_class"] == "HP"
                    and bool(job.get("evaluation_population"))
                ]
                projected_terminal_queue_p95 = _quantile(
                    completed_hp_queue + candidate_hp_queue,
                    0.95,
                )
                scenario_evidence.append(
                    {
                        "scenario_profile_id": scenario_run["scenario_profile_id"],
                        "selection_role": (
                            "stress"
                            if scenario_run["scenario_profile_id"]
                            == "recent-history-replay"
                            else "decision"
                        ),
                        "result_fingerprint": metric["run_spec_fingerprint"],
                        "metrics_fingerprint": metric["metrics_fingerprint"],
                        "audit_fingerprint": audit["audit_fingerprint"],
                        "hard_slo_passed": audit["audit_passed"],
                        "allocation_rate_mean": metric["cluster"][
                            "allocation_rate_mean"
                        ],
                        "hp_jct_p95_seconds": metric["jobs"]["HP"][
                            "jct_seconds"
                        ]["p95"],
                        "hp_queue_p95_seconds": metric["jobs"]["HP"][
                            "queue_seconds"
                        ]["p95"],
                        "projected_terminal_hp_queue_p95_seconds": (
                            round(projected_terminal_queue_p95, 6)
                            if projected_terminal_queue_p95 is not None
                            else None
                        ),
                        "prospective_queue_guard_passed": (
                            projected_terminal_queue_p95 is None
                            or projected_terminal_queue_p95
                            <= queue_threshold + EPSILON
                        ),
                        "spot_jct_p95_seconds": metric["jobs"]["Spot"][
                            "jct_seconds"
                        ]["p95"],
                        "spot_eviction_rate_per_run": metric[
                            "preemption_events"
                        ]["eviction_rate_per_run"],
                    }
                )

            decision_scenario_evidence = [
                item
                for item in scenario_evidence
                if item["selection_role"] == "decision"
            ]
            stress_scenario_evidence = [
                item
                for item in scenario_evidence
                if item["selection_role"] == "stress"
            ]

            def robust_value(field: str, *, minimize: bool) -> float | None:
                values = [
                    float(item[field])
                    for item in decision_scenario_evidence
                    if item[field] is not None
                ]
                if not values:
                    return None
                return min(values) if minimize else max(values)

            evidence_fingerprint = canonical_sha256(
                {
                    "scenario_profile_ids": [
                        item["scenario_profile_id"] for item in scenario_evidence
                    ],
                    "metrics_fingerprints": [
                        item["metrics_fingerprint"] for item in scenario_evidence
                    ],
                    "audit_fingerprints": [
                        item["audit_fingerprint"] for item in scenario_evidence
                    ],
                }
            )
            candidate_evidence.append(
                {
                    "action_id": policy.action_id,
                    "policy_fingerprint": policy.fingerprint,
                    "control_profile": _policy_payload(policy),
                    "metrics_fingerprint": evidence_fingerprint,
                    "audit_fingerprint": canonical_sha256(
                        [item["audit_fingerprint"] for item in scenario_evidence]
                    ),
                    "hard_slo_passed": all(
                        item["hard_slo_passed"] is True
                        and item["prospective_queue_guard_passed"] is True
                        for item in decision_scenario_evidence
                    ),
                    "stress_scenario_pass_count": sum(
                        item["hard_slo_passed"] is True
                        and item["prospective_queue_guard_passed"] is True
                        for item in stress_scenario_evidence
                    ),
                    "stress_scenario_count": len(stress_scenario_evidence),
                    "allocation_rate_mean": robust_value(
                        "allocation_rate_mean", minimize=True
                    ),
                    "hp_jct_p95_seconds": robust_value(
                        "hp_jct_p95_seconds", minimize=False
                    ),
                    "hp_queue_p95_seconds": robust_value(
                        "hp_queue_p95_seconds", minimize=False
                    ),
                    "spot_jct_p95_seconds": robust_value(
                        "spot_jct_p95_seconds", minimize=False
                    ),
                    "spot_eviction_rate_per_run": robust_value(
                        "spot_eviction_rate_per_run", minimize=False
                    ),
                    "robust_aggregation": {
                        "hard_slo": "must-pass-every-decision-scenario",
                        "stress_scenarios": (
                            "used only after the declared SLO hierarchy is tied"
                        ),
                        "prospective_queue_guard": (
                            "merge cutoff-completed HP queue values with each "
                            "candidate fork and enforce the declared p95 threshold"
                        ),
                        "allocation_rate_mean": "minimum-across-scenarios",
                        "latency_and_eviction": "maximum-across-scenarios",
                    },
                    "scenario_evidence": scenario_evidence,
                }
            )
        ranking = _rank_candidate_summaries(
            candidate_evidence,
            self.slo,
            self._active_action_id,
            self.baseline_action_id,
        )
        selected_action_id = ranking["selected_action_id"]
        hold_override = None
        if (
            selected_action_id == self.baseline_action_id
            and self._active_action_id != self.baseline_action_id
            and self._active_action_since is not None
            and now - self._active_action_since
            < self.minimum_action_hold_seconds - EPSILON
            and any(
                item["action_id"] == self._active_action_id
                and item["hard_slo_passed"] is True
                for item in candidate_evidence
            )
        ):
            hold_override = {
                "original_selected_action_id": selected_action_id,
                "retained_action_id": self._active_action_id,
                "active_for_seconds": now - self._active_action_since,
                "minimum_action_hold_seconds": self.minimum_action_hold_seconds,
                "reason": (
                    "Retain a still-eligible non-baseline action for the declared "
                    "minimum hold period to avoid one-interval control chatter."
                ),
            }
            selected_action_id = self._active_action_id
        decision: dict[str, Any] = {
            "decision_index": len(self._decisions),
            "cutoff_time_seconds": float(now),
            "execution_end_time_seconds": min(
                float(self._evaluation_end), now + self.decision_interval_seconds
            ),
            "previous_decision_fingerprint": (
                self._decisions[-1]["decision_fingerprint"]
                if self._decisions
                else None
            ),
            "scheduler_snapshot_fingerprint": scheduler_snapshot[
                "snapshot_fingerprint"
            ],
            "state_handoff_fingerprint": scheduler_snapshot[
                "state_handoff_fingerprint"
            ],
            "observation_fingerprint": observation["observation_fingerprint"],
            "cutoff_state_summary": {
                "regime": observation["multi_timescale_state"]["regime"],
                "change": observation["multi_timescale_state"]["change"],
                "runtime_pressure": observation["multi_timescale_state"][
                    "runtime_pressure"
                ],
                "forecast_risk": observation["multi_timescale_state"][
                    "forecast_risk"
                ],
                "maximum_hp_queue_wait_seconds": float(
                    scheduler_snapshot["queue"].get(
                        "maximum_hp_wait_seconds", 0.0
                    )
                ),
            },
            "predictive_projection_fingerprint": (
                observation["predictive_projection"]["projection_fingerprint"]
                if observation["predictive_projection"] is not None
                else None
            ),
            "candidate_selection": selection,
            "candidate_budget_consumed": len(candidate_ids),
            "candidate_simulation_count": len(candidate_ids) * len(scenario_runs),
            "scenario": {
                "scenario_set_id": (
                    "actual-future-oracle"
                    if self.use_actual_future
                    else self.scenario_set_id
                ),
                "scenario_count": len(scenario_runs),
                "scenario_profile_ids": [
                    item["scenario_profile_id"] for item in scenario_runs
                ],
                "scheduler_snapshot_fingerprint": scheduler_snapshot[
                    "snapshot_fingerprint"
                ],
                "future_arrivals_visible": self.use_actual_future,
                "carryover_runtime_estimator": (
                    "cutoff-visible-conditional-survival-median/v1"
                ),
                "queued_carryover_age_preserved": True,
                "robust_hard_slo_requires_every_scenario": True,
                "predictive_admission_mode_evaluated_per_action": True,
            },
            "scenarios": [
                {
                    "scenario_profile_id": item["scenario_profile_id"],
                    "trace_id": item["scenario"].trace_id,
                    "scenario_fingerprint": item["scenario"].fingerprint,
                    "forecast_derived_hp_jobs": item["scenario"].source.get(
                        "forecast_derived_hp_jobs", False
                    ),
                    "forecast_projection_fingerprint": item[
                        "scenario"
                    ].source.get("forecast_projection_fingerprint"),
                    "forecast_hp_decomposition": item["scenario"].source.get(
                        "forecast_hp_decomposition"
                    ),
                    "queued_carryover_job_count": len(
                        item["scenario"].source.get(
                            "initial_queue_wait_seconds_by_job_id", {}
                        )
                    ),
                }
                for item in scenario_runs
            ],
            "candidate_evidence": candidate_evidence,
            "ranking": ranking,
            "minimum_hold_override": hold_override,
            "selected_action_id": selected_action_id,
            "selected_policy_fingerprint": self.policies[
                selected_action_id
            ].fingerprint,
            "real_future_execution_frozen_after_selection": True,
        }
        decision["decision_fingerprint"] = canonical_sha256(decision)
        self._decisions.append(decision)
        if selected_action_id != self._active_action_id:
            self._active_action_since = float(now)
        elif self._active_action_since is None:
            self._active_action_since = float(now)
        self._active_action_id = selected_action_id
        self._next_decision_time = now + self.decision_interval_seconds
        if (
            self._evaluation_end is not None
            and self._next_decision_time >= self._evaluation_end - EPSILON
        ):
            self._next_decision_time = math.inf
        return self.policies[selected_action_id]

    def policy_descriptor(self) -> dict[str, Any]:
        return {
            **self._config,
            "action_id": self.controller_id,
            "scheduler": "rolling_high_level",
            "controller_fingerprint": self.fingerprint,
        }

    def planning_checkpoint(
        self, pending_observation: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a resumable receipt after the provider stops at a new cutoff."""

        value: dict[str, Any] = {
            "schema_version": "schednav.rolling-planning-checkpoint/v1",
            "controller_id": self.controller_id,
            "controller_fingerprint": self.fingerprint,
            "mode": self.mode,
            "completed_decision_count": len(self._decisions),
            "completed_decision_fingerprints": [
                item["decision_fingerprint"] for item in self._decisions
            ],
            "latest_completed_decision": (
                self._decisions[-1] if self._decisions else None
            ),
            "pending_observation": pending_observation,
            "pending_observation_fingerprint": pending_observation[
                "observation_fingerprint"
            ],
            "information_boundary": {
                "future_arrivals_visible": False,
                "real_execution_stopped_at_pending_cutoff": True,
            },
        }
        value["checkpoint_fingerprint"] = canonical_sha256(value)
        return value

    def finalize(self) -> dict[str, Any]:
        if self._evaluation_start is None or self._evaluation_end is None:
            raise ValueError("Rolling controller was never bound")
        expected = max(
            0,
            math.ceil(
                (self._evaluation_end - self._evaluation_start)
                / self.decision_interval_seconds
                - EPSILON
            ),
        )
        value: dict[str, Any] = {
            "schema_version": ROLLING_REPORT_SCHEMA,
            "controller_id": self.controller_id,
            "controller_fingerprint": self.fingerprint,
            "mode": self.mode,
            "model_id": self.provider.model_id,
            "evidence_window_seconds": {
                "start": self._evaluation_start,
                "end": self._evaluation_end,
            },
            "decision_count": len(self._decisions),
            "expected_decision_count": expected,
            "candidate_budget_per_decision": self.candidate_budget,
            "baseline_action_id": self.baseline_action_id,
            "candidate_scenario_set_id": self.scenario_set_id,
            "candidate_simulation_count": sum(
                item["candidate_simulation_count"] for item in self._decisions
            ),
            "llm_usage": {
                "call_count": sum(
                    item["candidate_selection"]["llm_call_count"]
                    for item in self._decisions
                ),
                "prompt_tokens": sum(
                    item["candidate_selection"]["prompt_tokens"]
                    for item in self._decisions
                ),
                "completion_tokens": sum(
                    item["candidate_selection"]["completion_tokens"]
                    for item in self._decisions
                ),
            },
            "information_boundary": {
                "agent_future_arrivals_visible": False,
                "candidate_actual_future_visible": self.use_actual_future,
                "candidate_scenarios": (
                    "post-hoc actual future; non-deployable upper bound"
                    if self.use_actual_future
                    else (
                        "cutoff-safe calibrated-P90 and recent-history forks plus "
                        "conditional carryover survival estimates"
                        if self.scenario_set_id == ROBUST_SCENARIO_SET_ID
                        else "completed-history replay plus past-estimated carryover"
                    )
                ),
                "real_future_executed_only_after_each_selection": True,
            },
            "state_handoff": {
                "single_simulator_session": True,
                "state_reinitialized_between_cutoffs": False,
                "preserved_state": [
                    "running allocations",
                    "queue order and wait",
                    "remaining work",
                    "preemption and Spot-run ledgers",
                    "predictive model and quota feedback",
                ],
                "decision_state_fingerprints": [
                    item["state_handoff_fingerprint"] for item in self._decisions
                ],
            },
            "selected_action_sequence": [
                item["selected_action_id"] for item in self._decisions
            ],
            "decisions": self._decisions,
            "weighted_score_used": False,
        }
        value["control_fingerprint"] = canonical_sha256(value)
        return value


class CatalogOracleCandidateProvider:
    """Marker provider for the explicitly non-deployable post-hoc oracle."""

    provider_id = "post-hoc-catalog-oracle-v1"
    model_id = None

    def select_candidates(
        self,
        observation: dict[str, Any],
        action_catalog: list[dict[str, Any]],
        candidate_budget: int,
    ) -> dict[str, Any]:
        raise RuntimeError("Catalog oracle candidate selection is built internally")
