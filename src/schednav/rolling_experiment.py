"""Reproducible rolling-control execution and ablation helpers.

This module owns experiment wiring, not scheduling policy.  The simulator keeps
the authoritative queue/runtime state, the predictive controller owns Spot
quota, and :mod:`schednav.rolling_control` owns bounded high-level decisions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import canonical_sha256
from .controller_factory import create_predictive_controller, load_controller_config
from .native_simulator import SimulationPolicy, build_metrics_report, simulate_trace
from .native_trace import CanonicalTrace, load_canonical_trace
from .rolling_control import (
    BASELINE_ACTION_ID,
    DEFAULT_SCENARIO_SET_ID,
    CatalogOracleCandidateProvider,
    IncrementalAgentCandidateProvider,
    RecordedAgentCandidateProvider,
    RollingDecisionRequired,
    RollingPolicyController,
    WorkloadRuleCandidateProvider,
    build_agent_plan,
)
from .slo import audit_slo_reports


ROLLING_ATTEMPT_SCHEMA = "schednav.rolling-attempt/v1"
ROLLING_RUN_SCHEMA = "schednav.rolling-run/v1"


def load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def load_rolling_action_space(
    project_root: Path, action_space_path: Path
) -> tuple[list[SimulationPolicy], str]:
    """Load registered profiles and their explicit rolling safety baseline."""

    project_root = project_root.resolve()
    value = load_json_object(action_space_path.resolve())
    if value.get("schema_version") != "schednav.native-action-space/v1":
        raise ValueError("Rolling experiments require a native action-space document")
    profiles = value.get("profiles")
    if not isinstance(profiles, list) or not 3 <= len(profiles) <= 5:
        raise ValueError("Rolling action space must register three to five profiles")
    policies: list[SimulationPolicy] = []
    for raw_path in profiles:
        relative = Path(str(raw_path))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Action-space profile paths must be project-relative")
        path = (project_root / relative).resolve()
        if project_root not in path.parents or not path.is_file():
            raise ValueError("Action-space profile escapes the project root")
        policies.append(SimulationPolicy.load(path))
    action_ids = [policy.action_id for policy in policies]
    baseline_action_id = str(value.get("safety_baseline_action_id", BASELINE_ACTION_ID))
    if len(set(action_ids)) != len(action_ids) or baseline_action_id not in action_ids:
        raise ValueError("Rolling catalog requires unique profiles and its safety baseline")
    baseline = next(
        policy for policy in policies if policy.action_id == baseline_action_id
    )
    if baseline.scheduler != "fifo":
        raise ValueError("Rolling safety baseline must use FIFO scheduling")
    if baseline_action_id != BASELINE_ACTION_ID and baseline.predictive_admission_mode != "bypass":
        raise ValueError("A non-default rolling safety baseline must bypass prediction")
    return policies, baseline_action_id


def load_policy_catalog(project_root: Path, action_space_path: Path) -> list[SimulationPolicy]:
    """Compatibility helper returning only the finite policy catalog."""

    policies, _baseline_action_id = load_rolling_action_space(
        project_root, action_space_path
    )
    return policies


def create_inner_controller(trace: CanonicalTrace, controller_path: Path) -> Any:
    """Create a fresh past-only predictive controller for one replay."""

    config = load_controller_config(controller_path)
    start = min(job.submit_time_seconds for job in trace.jobs)
    evidence_start = (
        trace.evaluation_start_seconds
        if trace.evaluation_start_seconds is not None
        else start
    )
    evidence_end = (
        trace.evaluation_end_seconds
        if trace.evaluation_end_seconds is not None
        else max(job.submit_time_seconds for job in trace.jobs)
    )
    return create_predictive_controller(
        config,
        trace,
        start,
        evidence_start_seconds=evidence_start,
        evidence_end_seconds=evidence_end,
    )


def _rolling_controller(
    *,
    trace: CanonicalTrace,
    policies: list[SimulationPolicy],
    slo: dict[str, Any],
    controller_id: str,
    mode: str,
    provider: Any,
    decision_interval_seconds: int,
    scenario_horizon_seconds: int,
    history_window_seconds: int,
    candidate_budget: int,
    use_actual_future: bool = False,
    workload_trace: CanonicalTrace | None = None,
    baseline_action_id: str = BASELINE_ACTION_ID,
    scenario_set_id: str = DEFAULT_SCENARIO_SET_ID,
    minimum_action_hold_seconds: int = 0,
) -> RollingPolicyController:
    return RollingPolicyController(
        controller_id=controller_id,
        mode=mode,
        trace=trace,
        policies=policies,
        slo=slo,
        candidate_provider=provider,
        decision_interval_seconds=decision_interval_seconds,
        scenario_horizon_seconds=scenario_horizon_seconds,
        history_window_seconds=history_window_seconds,
        candidate_budget=candidate_budget,
        use_actual_future=use_actual_future,
        workload_trace=workload_trace,
        baseline_action_id=baseline_action_id,
        scenario_set_id=scenario_set_id,
        minimum_action_hold_seconds=minimum_action_hold_seconds,
    )


def _baseline_evidence(
    trace: CanonicalTrace,
    policies: list[SimulationPolicy],
    baseline_action_id: str = BASELINE_ACTION_ID,
) -> tuple[dict[str, Any], dict[str, Any]]:
    baseline = next(
        policy for policy in policies if policy.action_id == baseline_action_id
    )
    result = simulate_trace(trace, baseline)
    return result, build_metrics_report(result)


def run_incremental_agent_attempt(
    *,
    project_root: Path,
    trace_path: Path,
    action_space_path: Path,
    predictive_controller_path: Path,
    slo_path: Path,
    rolling_controller_id: str,
    mode: str,
    source_project_id: str,
    decisions: list[dict[str, Any]],
    workload_history_trace_path: Path | None = None,
    decision_interval_seconds: int = 14400,
    scenario_horizon_seconds: int = 14400,
    history_window_seconds: int = 14400,
    candidate_budget: int = 3,
    scenario_set_id: str = DEFAULT_SCENARIO_SET_ID,
    minimum_action_hold_seconds: int = 0,
) -> dict[str, Any]:
    """Replay accepted AgentTeams decisions and stop at the first unseen cutoff.

    The final successful replay is one simulator session with exact state
    handoff.  Earlier calls are deterministic planning replays whose sole
    purpose is to reveal the next cutoff-safe observation.
    """

    if mode not in {"single_agent", "multi_agent", "multi_agent_masked"}:
        raise ValueError("Incremental Agent mode is unsupported")
    trace = load_canonical_trace(trace_path)
    workload_trace = (
        load_canonical_trace(workload_history_trace_path)
        if workload_history_trace_path is not None
        else trace
    )
    policies, baseline_action_id = load_rolling_action_space(
        project_root, action_space_path
    )
    slo = load_json_object(slo_path)
    provider = IncrementalAgentCandidateProvider(
        controller_id=rolling_controller_id,
        mode=mode,
        decisions=decisions,
        source_project_id=source_project_id,
        baseline_action_id=baseline_action_id,
    )
    rolling = _rolling_controller(
        trace=trace,
        policies=policies,
        slo=slo,
        controller_id=rolling_controller_id,
        mode=mode,
        provider=provider,
        decision_interval_seconds=decision_interval_seconds,
        scenario_horizon_seconds=scenario_horizon_seconds,
        history_window_seconds=history_window_seconds,
        candidate_budget=candidate_budget,
        workload_trace=workload_trace,
        baseline_action_id=baseline_action_id,
        scenario_set_id=scenario_set_id,
        minimum_action_hold_seconds=minimum_action_hold_seconds,
    )
    initial_policy = next(
        policy for policy in policies if policy.action_id == baseline_action_id
    )
    try:
        result = simulate_trace(
            trace,
            initial_policy,
            create_inner_controller(trace, predictive_controller_path),
            rolling_controller=rolling,
        )
    except RollingDecisionRequired as required:
        checkpoint = rolling.planning_checkpoint(required.observation)
        value: dict[str, Any] = {
            "schema_version": ROLLING_ATTEMPT_SCHEMA,
            "status": "decision_required",
            "mode": mode,
            "trace_id": trace.trace_id,
            "trace_fingerprint": trace.fingerprint,
            "rolling_controller_id": rolling_controller_id,
            "accepted_decision_count": len(decisions),
            "checkpoint": checkpoint,
        }
        value["attempt_fingerprint"] = canonical_sha256(value)
        return value

    metrics = build_metrics_report(result)
    if len(decisions) != result["rolling_control"]["decision_count"]:
        raise ValueError("Agent plan contains decisions outside the evidence window")
    baseline_result, baseline_metrics = _baseline_evidence(
        trace, policies, baseline_action_id
    )
    audit = audit_slo_reports(metrics, slo, baseline_metrics)
    plan = build_agent_plan(
        controller_id=rolling_controller_id,
        mode=mode,
        decisions=decisions,
        source_project_id=source_project_id,
        baseline_action_id=baseline_action_id,
    )
    value = {
        "schema_version": ROLLING_ATTEMPT_SCHEMA,
        "status": "completed",
        "mode": mode,
        "trace_id": trace.trace_id,
        "trace_fingerprint": trace.fingerprint,
        "rolling_controller_id": rolling_controller_id,
        "accepted_decision_count": len(decisions),
        "agent_plan": plan,
        "simulation_result": result,
        "metrics": metrics,
        "slo_audit": audit,
        "fifo_baseline_result": baseline_result,
        "fifo_baseline_metrics": baseline_metrics,
    }
    value["attempt_fingerprint"] = canonical_sha256(value)
    return value


def run_static_arm(
    trace: CanonicalTrace,
    policy: SimulationPolicy,
    *,
    predictive_controller_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    predictor = (
        create_inner_controller(trace, predictive_controller_path)
        if predictive_controller_path is not None
        else None
    )
    result = simulate_trace(trace, policy, predictor)
    return result, build_metrics_report(result)


def run_rolling_arm(
    *,
    project_root: Path,
    trace_path: Path,
    action_space_path: Path,
    predictive_controller_path: Path,
    slo_path: Path,
    arm_id: str,
    mode: str,
    agent_plan: dict[str, Any] | None = None,
    decision_interval_seconds: int = 14400,
    scenario_horizon_seconds: int = 14400,
    history_window_seconds: int = 14400,
    candidate_budget: int = 3,
    workload_history_trace_path: Path | None = None,
    scenario_set_id: str = DEFAULT_SCENARIO_SET_ID,
    minimum_action_hold_seconds: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one completed deployable rolling arm or post-hoc oracle."""

    trace = load_canonical_trace(trace_path)
    workload_trace = (
        load_canonical_trace(workload_history_trace_path)
        if workload_history_trace_path is not None
        else trace
    )
    policies, baseline_action_id = load_rolling_action_space(
        project_root, action_space_path
    )
    slo = load_json_object(slo_path)
    use_actual_future = mode == "catalog_oracle"
    if mode == "workload_rule":
        provider: Any = WorkloadRuleCandidateProvider(baseline_action_id)
    elif mode in {"single_agent", "multi_agent", "multi_agent_masked"}:
        if agent_plan is None:
            raise ValueError("Agent rolling arms require a completed plan")
        if agent_plan.get("controller_id") != arm_id or agent_plan.get("mode") != mode:
            raise ValueError("Agent plan identity must match the requested rolling arm")
        provider = RecordedAgentCandidateProvider(agent_plan, baseline_action_id)
    elif mode == "catalog_oracle":
        provider = CatalogOracleCandidateProvider()
        candidate_budget = len(policies)
    else:
        raise ValueError("Unsupported rolling arm mode")
    rolling = _rolling_controller(
        trace=trace,
        policies=policies,
        slo=slo,
        controller_id=arm_id,
        mode=mode,
        provider=provider,
        decision_interval_seconds=decision_interval_seconds,
        scenario_horizon_seconds=scenario_horizon_seconds,
        history_window_seconds=history_window_seconds,
        candidate_budget=candidate_budget,
        use_actual_future=use_actual_future,
        workload_trace=workload_trace,
        baseline_action_id=baseline_action_id,
        scenario_set_id=scenario_set_id,
        minimum_action_hold_seconds=minimum_action_hold_seconds,
    )
    initial_policy = next(
        policy for policy in policies if policy.action_id == baseline_action_id
    )
    result = simulate_trace(
        trace,
        initial_policy,
        create_inner_controller(trace, predictive_controller_path),
        rolling_controller=rolling,
    )
    return result, build_metrics_report(result)


def build_rolling_run_spec(
    *,
    trace_path: Path,
    trace: CanonicalTrace,
    rolling_report: dict[str, Any],
    predictive_controller_path: Path,
    action_space_path: Path,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": ROLLING_RUN_SCHEMA,
        "trace_manifest": trace_path.as_posix(),
        "trace_fingerprint": trace.fingerprint,
        "action_space": action_space_path.as_posix(),
        "rolling_controller_id": rolling_report["controller_id"],
        "rolling_controller_fingerprint": rolling_report[
            "controller_fingerprint"
        ],
        "predictive_controller": predictive_controller_path.as_posix(),
        "information_boundary": rolling_report["information_boundary"],
    }
    value["run_fingerprint"] = canonical_sha256(value)
    return value
