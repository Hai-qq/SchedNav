"""Leakage-resistant adaptive-controller design and evaluation helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any

from .contracts import canonical_sha256


DESIGN_SCHEMA = "schednav.adaptive-study-design/v1"
CONTROLLER_SCHEMA = "schednav.controller-selections/v1"
BENCHMARK_SCHEMA = "schednav.adaptive-benchmark/v1"
EVIDENCE_SCHEMA = "schednav.adaptive-evidence/v1"
BASELINE_ACTION_ID = "native-fifo"
EPSILON = 1e-12


def _verified(value: dict[str, Any], fingerprint_field: str) -> bool:
    supplied = value.get(fingerprint_field)
    payload = {key: item for key, item in value.items() if key != fingerprint_field}
    return isinstance(supplied, str) and canonical_sha256(payload) == supplied


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 9) if values else None


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 9)


def _action_payload(policy: dict[str, Any]) -> dict[str, Any]:
    excluded = {"schema_version", "preemption_overhead_seconds", "placement_strategy"}
    return {key: value for key, value in policy.items() if key not in excluded}


def build_adaptive_design(
    selection: dict[str, Any],
    action_space: dict[str, Any],
    policies: list[dict[str, Any]],
    *,
    action_space_path: str,
    time_origin: str,
    gpu_model: str,
    calibration_fraction: float = 0.6,
) -> dict[str, Any]:
    """Freeze a chronological split and workload-only controller inputs."""
    if not _verified(selection, "selection_fingerprint"):
        raise ValueError("Selection fingerprint is invalid")
    if selection.get("selection_method", {}).get("name") != "all-eligible-origin-aligned":
        raise ValueError("Adaptive studies require an all-eligible window selection")
    if not 0.5 <= calibration_fraction < 1:
        raise ValueError("calibration_fraction must be in [0.5, 1)")
    selected = sorted(
        selection["selected_windows"],
        key=lambda item: float(item["window_seconds"]["start"]),
    )
    if len(selected) < 4:
        raise ValueError("Adaptive studies require at least four eligible windows")
    cut = int(len(selected) * calibration_fraction)
    if cut <= 0 or cut >= len(selected):
        raise ValueError("Chronological split produced an empty partition")
    action_ids = [str(policy["action_id"]) for policy in policies]
    if len(action_ids) != len(set(action_ids)) or not 3 <= len(action_ids) <= 5:
        raise ValueError("Adaptive action spaces require three to five unique actions")
    if BASELINE_ACTION_ID not in action_ids:
        raise ValueError("Adaptive action spaces must include native-fifo")
    origin = datetime.fromisoformat(time_origin)
    windows: list[dict[str, Any]] = []
    for index, item in enumerate(selected):
        start = float(item["window_seconds"]["start"])
        date = (origin + timedelta(seconds=start)).date().isoformat()
        windows.append(
            {
                "window_id": f"{gpu_model}-{date}",
                "date": date,
                "partition": "calibration" if index < cut else "evaluation",
                "window_seconds": item["window_seconds"],
                "population": item["population"],
                "regime_signals": {
                    "spot_requested_gpu_share": item["spot_requested_gpu_share"],
                    "combined_peak_active_pressure": item[
                        "combined_peak_active_pressure"
                    ],
                    "combined_mean_active_pressure": item[
                        "combined_mean_active_pressure"
                    ],
                },
            }
        )
    calibration = [item for item in windows if item["partition"] == "calibration"]
    pressure_threshold = float(
        median(
            item["regime_signals"]["combined_peak_active_pressure"]
            for item in calibration
        )
    )
    spot_share_threshold = float(
        median(
            item["regime_signals"]["spot_requested_gpu_share"]
            for item in calibration
        )
    )
    report: dict[str, Any] = {
        "schema_version": DESIGN_SCHEMA,
        "trace": {
            "trace_id": selection["trace_id"],
            "trace_fingerprint": selection["trace_fingerprint"],
            "source": selection["source"],
            "capacity_gpus": selection["capacity_gpus"],
            "gpu_model": gpu_model,
            "time_origin": time_origin,
        },
        "selection_fingerprint": selection["selection_fingerprint"],
        "action_space": {
            "path": action_space_path,
            "name": action_space["name"],
            "fingerprint": canonical_sha256(action_space),
            "actions": [_action_payload(policy) for policy in policies],
            "candidate_count_rule": {"minimum": 3, "maximum": 5},
        },
        "split": {
            "method": "chronological-prefix-calibration",
            "calibration_fraction": calibration_fraction,
            "calibration_window_count": cut,
            "evaluation_window_count": len(windows) - cut,
            "evaluation_not_visible_to_calibration_selection": True,
        },
        "workload_rule_thresholds": {
            "source": "calibration-window workload medians; no simulation metrics",
            "combined_peak_active_pressure": pressure_threshold,
            "spot_requested_gpu_share": spot_share_threshold,
        },
        "windows": windows,
        "definition": (
            "The chronological split, bounded action catalog, workload signals and "
            "rule thresholds are frozen before candidate-policy simulation. Agent "
            "selection may use these fields but not evaluation outcomes."
        ),
    }
    report["design_fingerprint"] = canonical_sha256(report)
    return report


def build_rule_controller(design: dict[str, Any]) -> dict[str, Any]:
    """Build a transparent three-candidate workload-only controller."""
    if design.get("schema_version") != DESIGN_SCHEMA or not _verified(
        design, "design_fingerprint"
    ):
        raise ValueError("Adaptive design is invalid")
    action_ids = {
        str(action["action_id"])
        for action in design["action_space"]["actions"]
    }
    expected = {
        BASELINE_ACTION_ID,
        "native-preemptive-3600",
        "native-preemptive-g3600-b09-d0000",
        "native-preemptive-g3600-b09-d0900",
        "native-preemptive-g3600-b09-loss-aware",
    }
    if action_ids != expected:
        raise ValueError("workload-rule-v1 requires the native multiwindow v3 catalog")
    pressure_threshold = float(
        design["workload_rule_thresholds"]["combined_peak_active_pressure"]
    )
    spot_threshold = float(
        design["workload_rule_thresholds"]["spot_requested_gpu_share"]
    )
    windows: list[dict[str, Any]] = []
    for window in design["windows"]:
        if window["partition"] != "evaluation":
            continue
        pressure = float(
            window["regime_signals"]["combined_peak_active_pressure"]
        )
        spot_share = float(
            window["regime_signals"]["spot_requested_gpu_share"]
        )
        if pressure >= pressure_threshold and spot_share >= spot_threshold:
            reason_code = "high-pressure-high-spot-share"
            candidates = [
                BASELINE_ACTION_ID,
                "native-preemptive-g3600-b09-d0000",
                "native-preemptive-g3600-b09-loss-aware",
            ]
        elif pressure >= pressure_threshold:
            reason_code = "high-pressure-lower-spot-share"
            candidates = [
                BASELINE_ACTION_ID,
                "native-preemptive-3600",
                "native-preemptive-g3600-b09-d0000",
            ]
        elif spot_share >= spot_threshold:
            reason_code = "lower-pressure-high-spot-share"
            candidates = [
                BASELINE_ACTION_ID,
                "native-preemptive-g3600-b09-d0900",
                "native-preemptive-g3600-b09-loss-aware",
            ]
        else:
            reason_code = "lower-pressure-lower-spot-share"
            candidates = [
                BASELINE_ACTION_ID,
                "native-preemptive-g3600-b09-d0000",
                "native-preemptive-g3600-b09-d0900",
            ]
        windows.append(
            {
                "window_id": window["window_id"],
                "candidate_action_ids": candidates,
                "reason_code": reason_code,
            }
        )
    controller: dict[str, Any] = {
        "schema_version": CONTROLLER_SCHEMA,
        "controller_id": "workload-rule-v1",
        "design_fingerprint": design["design_fingerprint"],
        "selection_basis": "workload_only",
        "model_id": None,
        "windows": windows,
        "definition": (
            "A pre-registered workload rule chooses exactly three bounded candidates. "
            "It does not inspect simulation metrics or choose placement."
        ),
    }
    controller["controller_fingerprint"] = canonical_sha256(controller)
    return controller


def validate_controller_selections(
    controller: dict[str, Any], design: dict[str, Any]
) -> None:
    if controller.get("schema_version") != CONTROLLER_SCHEMA or not _verified(
        controller, "controller_fingerprint"
    ):
        raise ValueError("Controller selection artifact is invalid")
    if controller.get("design_fingerprint") != design.get("design_fingerprint"):
        raise ValueError("Controller and adaptive design fingerprints differ")
    allowed = {
        str(action["action_id"])
        for action in design["action_space"]["actions"]
    }
    expected_windows = {
        str(window["window_id"])
        for window in design["windows"]
        if window["partition"] == "evaluation"
    }
    supplied_windows = [str(item.get("window_id")) for item in controller.get("windows", [])]
    if len(supplied_windows) != len(set(supplied_windows)) or set(supplied_windows) != expected_windows:
        raise ValueError("Controller must cover every evaluation window exactly once")
    for item in controller["windows"]:
        candidates = [str(value) for value in item.get("candidate_action_ids", [])]
        if not 3 <= len(candidates) <= 5 or len(candidates) != len(set(candidates)):
            raise ValueError("Every controller window requires three to five unique actions")
        if not set(candidates).issubset(allowed):
            raise ValueError("Controller selected an action outside the bounded catalog")
        if BASELINE_ACTION_ID not in candidates:
            raise ValueError("Every controller candidate set must include native-fifo")


def _policy_frontier(policies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    remaining = [item for item in policies if item["hard_slo_passed"] is True]
    if not remaining:
        return []
    best_allocation = max(float(item["allocation_rate_mean"]) for item in remaining)
    remaining = [
        item
        for item in remaining
        if best_allocation - float(item["allocation_rate_mean"]) < 0.01
    ]
    if len(remaining) > 1:
        best_spot_jct = min(float(item["spot_jct_p95_seconds"]) for item in remaining)
        remaining = [
            item
            for item in remaining
            if float(item["spot_jct_p95_seconds"]) == best_spot_jct
        ]
    if len(remaining) > 1:
        best_eviction = min(
            float(item["spot_eviction_rate_per_run"]) for item in remaining
        )
        remaining = [
            item
            for item in remaining
            if float(item["spot_eviction_rate_per_run"]) == best_eviction
        ]
    return remaining


def _select_static_action(
    calibration_records: list[dict[str, Any]], action_ids: list[str]
) -> tuple[str | None, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for action_id in action_ids:
        policies = [
            next(
                policy
                for policy in record["policies"]
                if policy["action_id"] == action_id
            )
            for record in calibration_records
        ]
        eligible = [item for item in policies if item["hard_slo_passed"] is True]
        stats[action_id] = {
            "hard_slo_pass_window_count": len(eligible),
            "allocation_rate_mean_on_hard_pass": _mean(
                [float(item["allocation_rate_mean"]) for item in eligible]
            ),
            "spot_jct_p95_mean_on_hard_pass": _mean(
                [float(item["spot_jct_p95_seconds"]) for item in eligible]
            ),
            "spot_eviction_rate_mean_on_hard_pass": _mean(
                [float(item["spot_eviction_rate_per_run"]) for item in eligible]
            ),
        }
    remaining = list(action_ids)
    best_pass = max(stats[action]["hard_slo_pass_window_count"] for action in remaining)
    remaining = [
        action
        for action in remaining
        if stats[action]["hard_slo_pass_window_count"] == best_pass
    ]
    best_allocation = max(
        float(stats[action]["allocation_rate_mean_on_hard_pass"] or 0.0)
        for action in remaining
    )
    remaining = [
        action
        for action in remaining
        if float(stats[action]["allocation_rate_mean_on_hard_pass"] or 0.0)
        == best_allocation
    ]
    if len(remaining) > 1:
        best_jct = min(
            float(stats[action]["spot_jct_p95_mean_on_hard_pass"] or float("inf"))
            for action in remaining
        )
        remaining = [
            action
            for action in remaining
            if float(stats[action]["spot_jct_p95_mean_on_hard_pass"] or float("inf"))
            == best_jct
        ]
    if len(remaining) > 1:
        best_eviction = min(
            float(stats[action]["spot_eviction_rate_mean_on_hard_pass"] or 0.0)
            for action in remaining
        )
        remaining = [
            action
            for action in remaining
            if float(stats[action]["spot_eviction_rate_mean_on_hard_pass"] or 0.0)
            == best_eviction
        ]
    report = {
        "selection_status": "selected" if len(remaining) == 1 else "tie_requires_human_approval",
        "selected_action_ids": remaining,
        "per_action": stats,
        "hierarchy": [
            "maximize calibration hard-SLO pass-window count",
            "maximize mean allocation on hard-pass calibration windows",
            "minimize mean Spot p95 JCT on hard-pass calibration windows",
            "minimize mean Spot eviction rate on hard-pass calibration windows",
            "preserve any remaining tie for human approval",
        ],
    }
    return (remaining[0] if len(remaining) == 1 else None), report


def _controller_summary(
    controller_id: str,
    evaluation_records: list[dict[str, Any]],
    candidate_map: dict[str, list[str]],
    all_action_ids: list[str],
) -> dict[str, Any]:
    windows: list[dict[str, Any]] = []
    candidate_set_regrets: list[float] = []
    candidate_set_uplifts: list[float] = []
    selected_uplift_lower_bounds: list[float] = []
    selected_uplift_upper_bounds: list[float] = []
    selected_spot_jct_deltas: list[float] = []
    selected_spot_eviction_deltas: list[float] = []
    candidate_counts: list[int] = []
    oracle_feasible_window_count = 0
    for record in evaluation_records:
        by_action = {
            str(policy["action_id"]): policy for policy in record["policies"]
        }
        candidates = candidate_map[str(record["window_id"])]
        candidate_counts.append(len(candidates))
        candidate_policies = [by_action[action] for action in candidates]
        eligible = [
            policy for policy in candidate_policies if policy["hard_slo_passed"] is True
        ]
        oracle_eligible = [
            by_action[action]
            for action in all_action_ids
            if by_action[action]["hard_slo_passed"] is True
        ]
        frontier = _policy_frontier(candidate_policies)
        oracle_frontier = _policy_frontier(oracle_eligible)
        best_candidate = (
            max(float(item["allocation_rate_mean"]) for item in eligible)
            if eligible
            else None
        )
        best_oracle = (
            max(float(item["allocation_rate_mean"]) for item in oracle_eligible)
            if oracle_eligible
            else None
        )
        if best_oracle is not None:
            oracle_feasible_window_count += 1
        fifo_allocation = float(by_action[BASELINE_ACTION_ID]["allocation_rate_mean"])
        fifo_spot_jct = float(by_action[BASELINE_ACTION_ID]["spot_jct_p95_seconds"])
        fifo_spot_eviction = float(
            by_action[BASELINE_ACTION_ID]["spot_eviction_rate_per_run"]
        )
        candidate_set_regret = (
            max(0.0, best_oracle - best_candidate)
            if best_candidate is not None and best_oracle is not None
            else None
        )
        candidate_set_uplift = (
            best_candidate - fifo_allocation if best_candidate is not None else None
        )
        selected_allocations = [
            float(item["allocation_rate_mean"]) for item in frontier
        ]
        selected_allocation_range = (
            {
                "minimum": min(selected_allocations),
                "maximum": max(selected_allocations),
            }
            if selected_allocations
            else None
        )
        selected_uplift_range = (
            {
                "lower_bound": selected_allocation_range["minimum"]
                - fifo_allocation,
                "upper_bound": selected_allocation_range["maximum"]
                - fifo_allocation,
            }
            if selected_allocation_range is not None
            else None
        )
        selected_spot_jct = (
            float(frontier[0]["spot_jct_p95_seconds"]) if frontier else None
        )
        selected_spot_eviction = (
            float(frontier[0]["spot_eviction_rate_per_run"])
            if frontier
            else None
        )
        frontier_action_ids = {str(item["action_id"]) for item in frontier}
        oracle_frontier_action_ids = {
            str(item["action_id"]) for item in oracle_frontier
        }
        if candidate_set_regret is not None:
            candidate_set_regrets.append(candidate_set_regret)
        if candidate_set_uplift is not None:
            candidate_set_uplifts.append(candidate_set_uplift)
        if selected_uplift_range is not None:
            selected_uplift_lower_bounds.append(selected_uplift_range["lower_bound"])
            selected_uplift_upper_bounds.append(selected_uplift_range["upper_bound"])
            selected_spot_jct_deltas.append(selected_spot_jct - fifo_spot_jct)
            selected_spot_eviction_deltas.append(
                selected_spot_eviction - fifo_spot_eviction
            )
        status = (
            "no_eligible_policy"
            if not frontier
            else "selected"
            if len(frontier) == 1
            else "tie_requires_human_approval"
        )
        windows.append(
            {
                "window_id": record["window_id"],
                "date": record["date"],
                "candidate_action_ids": candidates,
                "hard_slo_eligible_action_ids": [
                    str(item["action_id"]) for item in eligible
                ],
                "selection_status": status,
                "selected_action_ids": sorted(frontier_action_ids),
                "selected_allocation_rate_range": selected_allocation_range,
                "selected_allocation_uplift_vs_fifo_range": selected_uplift_range,
                "selected_spot_jct_p95_seconds": selected_spot_jct,
                "selected_spot_eviction_rate_per_run": selected_spot_eviction,
                "catalog_oracle_frontier_action_ids": sorted(
                    oracle_frontier_action_ids
                ),
                "catalog_oracle_frontier_covered": bool(
                    frontier_action_ids & oracle_frontier_action_ids
                ),
                "catalog_oracle_frontier_exact_match": bool(
                    oracle_frontier_action_ids
                    and frontier_action_ids == oracle_frontier_action_ids
                ),
                "candidate_set_best_hard_pass_allocation_rate": best_candidate,
                "candidate_set_best_allocation_uplift_vs_fifo": candidate_set_uplift,
                "candidate_set_allocation_regret_vs_catalog_best": candidate_set_regret,
                "candidate_set_catalog_best_allocation_covered": (
                    candidate_set_regret is not None
                    and candidate_set_regret <= EPSILON
                ),
            }
        )
    summary: dict[str, Any] = {
        "controller_id": controller_id,
        "evaluation_window_count": len(windows),
        "hard_slo_feasible_window_count": sum(
            item["selected_allocation_rate_range"] is not None for item in windows
        ),
        "catalog_oracle_feasible_window_count": oracle_feasible_window_count,
        "catalog_oracle_frontier_coverage_window_count": sum(
            item["catalog_oracle_frontier_covered"] is True for item in windows
        ),
        "catalog_oracle_frontier_coverage_rate_on_feasible_windows": round(
            sum(
                item["catalog_oracle_frontier_covered"] is True
                for item in windows
            )
            / oracle_feasible_window_count,
            9,
        )
        if oracle_feasible_window_count
        else None,
        "catalog_oracle_frontier_exact_match_window_count": sum(
            item["catalog_oracle_frontier_exact_match"] is True
            for item in windows
        ),
        "catalog_oracle_frontier_exact_match_rate_on_feasible_windows": round(
            sum(
                item["catalog_oracle_frontier_exact_match"] is True
                for item in windows
            )
            / oracle_feasible_window_count,
            9,
        )
        if oracle_feasible_window_count
        else None,
        "candidate_policy_evaluation_count": sum(candidate_counts),
        "candidate_count_per_window": {
            "mean": _mean([float(value) for value in candidate_counts]),
            "min": min(candidate_counts),
            "max": max(candidate_counts),
        },
        "candidate_policy_evaluation_reduction_vs_catalog_oracle": round(
            1
            - sum(candidate_counts)
            / (len(evaluation_records) * len(all_action_ids)),
            9,
        ),
        "selection_status_counts": {
            status: sum(item["selection_status"] == status for item in windows)
            for status in (
                "selected",
                "tie_requires_human_approval",
                "no_eligible_policy",
            )
        },
        "selected_allocation_uplift_vs_fifo": {
            "mean_lower_bound": _mean(selected_uplift_lower_bounds),
            "mean_upper_bound": _mean(selected_uplift_upper_bounds),
            "maximum_upper_bound": (
                max(selected_uplift_upper_bounds)
                if selected_uplift_upper_bounds
                else None
            ),
            "lower_bound_positive_window_count": sum(
                value > EPSILON for value in selected_uplift_lower_bounds
            ),
            "lower_bound_equal_window_count": sum(
                abs(value) <= EPSILON for value in selected_uplift_lower_bounds
            ),
            "lower_bound_negative_window_count": sum(
                value < -EPSILON for value in selected_uplift_lower_bounds
            ),
        },
        "selected_spot_jct_delta_vs_fifo_seconds": {
            "mean": _mean(selected_spot_jct_deltas),
            "p95": _quantile(selected_spot_jct_deltas, 0.95),
            "minimum": min(selected_spot_jct_deltas)
            if selected_spot_jct_deltas
            else None,
            "maximum": max(selected_spot_jct_deltas)
            if selected_spot_jct_deltas
            else None,
        },
        "selected_spot_eviction_rate_delta_vs_fifo": {
            "mean": _mean(selected_spot_eviction_deltas),
            "p95": _quantile(selected_spot_eviction_deltas, 0.95),
            "minimum": min(selected_spot_eviction_deltas)
            if selected_spot_eviction_deltas
            else None,
            "maximum": max(selected_spot_eviction_deltas)
            if selected_spot_eviction_deltas
            else None,
        },
        "candidate_set_catalog_best_allocation_coverage_window_count": sum(
            item["candidate_set_catalog_best_allocation_covered"] is True
            for item in windows
        ),
        "candidate_set_catalog_best_allocation_coverage_rate_on_feasible_windows": round(
            sum(
                item["candidate_set_catalog_best_allocation_covered"] is True
                for item in windows
            )
            / oracle_feasible_window_count,
            9,
        )
        if oracle_feasible_window_count
        else None,
        "candidate_set_allocation_regret_vs_catalog_best": {
            "mean": _mean(candidate_set_regrets),
            "p95": _quantile(candidate_set_regrets, 0.95),
            "max": max(candidate_set_regrets) if candidate_set_regrets else None,
        },
        "candidate_set_best_allocation_uplift_vs_fifo": {
            "mean": _mean(candidate_set_uplifts),
            "p95": _quantile(candidate_set_uplifts, 0.95),
            "max": max(candidate_set_uplifts) if candidate_set_uplifts else None,
            "positive_window_count": sum(
                value > EPSILON for value in candidate_set_uplifts
            ),
            "equal_window_count": sum(
                abs(value) <= EPSILON for value in candidate_set_uplifts
            ),
            "negative_window_count": sum(
                value < -EPSILON for value in candidate_set_uplifts
            ),
        },
        "windows": windows,
    }
    summary["controller_fingerprint"] = canonical_sha256(summary)
    return summary


def evaluate_adaptive_benchmark(
    multiwindow_summary: dict[str, Any],
    design: dict[str, Any],
    rule_controller: dict[str, Any],
    agent_controller: dict[str, Any],
) -> dict[str, Any]:
    """Compare FIFO, a calibrated fixed policy, rule and AgentTeams controllers."""
    if not _verified(multiwindow_summary, "multiwindow_fingerprint"):
        raise ValueError("Multi-window summary fingerprint is invalid")
    if design.get("schema_version") != DESIGN_SCHEMA or not _verified(
        design, "design_fingerprint"
    ):
        raise ValueError("Adaptive design is invalid")
    if multiwindow_summary.get("selection_fingerprint") != design.get(
        "selection_fingerprint"
    ):
        raise ValueError("Experiment and adaptive design selections differ")
    validate_controller_selections(rule_controller, design)
    validate_controller_selections(agent_controller, design)
    if agent_controller.get("model_id") != "deepseek-v4-flash":
        raise ValueError("Agent controller must be produced by deepseek-v4-flash")
    design_partition = {
        str(window["window_id"]): str(window["partition"])
        for window in design["windows"]
    }
    records = sorted(multiwindow_summary["windows"], key=lambda item: item["date"])
    if {str(record["window_id"]) for record in records} != set(design_partition):
        raise ValueError("Experiment does not cover the frozen adaptive design")
    calibration_records = [
        record
        for record in records
        if design_partition[str(record["window_id"])] == "calibration"
    ]
    evaluation_records = [
        record
        for record in records
        if design_partition[str(record["window_id"])] == "evaluation"
    ]
    all_action_ids = sorted(
        str(policy["action_id"]) for policy in records[0]["policies"]
    )
    design_action_ids = sorted(
        str(action["action_id"])
        for action in design["action_space"]["actions"]
    )
    if all_action_ids != design_action_ids:
        raise ValueError("Experiment action catalog differs from the frozen design")
    static_action_id, static_selection = _select_static_action(
        calibration_records, all_action_ids
    )
    if static_action_id is None:
        raise ValueError("Best static policy remains tied and requires human approval")
    evaluation_ids = [str(record["window_id"]) for record in evaluation_records]
    rule_map = {
        str(item["window_id"]): [str(action) for action in item["candidate_action_ids"]]
        for item in rule_controller["windows"]
    }
    agent_map = {
        str(item["window_id"]): [str(action) for action in item["candidate_action_ids"]]
        for item in agent_controller["windows"]
    }
    controllers = {
        "fifo": _controller_summary(
            "fifo",
            evaluation_records,
            {window_id: [BASELINE_ACTION_ID] for window_id in evaluation_ids},
            all_action_ids,
        ),
        "best_static": _controller_summary(
            "best_static",
            evaluation_records,
            {window_id: [static_action_id] for window_id in evaluation_ids},
            all_action_ids,
        ),
        "workload_rule": _controller_summary(
            "workload_rule",
            evaluation_records,
            rule_map,
            all_action_ids,
        ),
        "agentteams": _controller_summary(
            "agentteams",
            evaluation_records,
            agent_map,
            all_action_ids,
        ),
        "catalog_oracle": _controller_summary(
            "catalog_oracle",
            evaluation_records,
            {window_id: all_action_ids for window_id in evaluation_ids},
            all_action_ids,
        ),
    }
    report: dict[str, Any] = {
        "schema_version": BENCHMARK_SCHEMA,
        "design_fingerprint": design["design_fingerprint"],
        "multiwindow_fingerprint": multiwindow_summary["multiwindow_fingerprint"],
        "rule_controller_fingerprint": rule_controller["controller_fingerprint"],
        "agent_controller_fingerprint": agent_controller["controller_fingerprint"],
        "split": design["split"],
        "calibration_static_selection": static_selection,
        "best_static_action_id": static_action_id,
        "controllers": controllers,
        "claim_boundary": (
            "Catalog oracle is an offline upper bound. Controller comparisons use "
            "only chronological evaluation windows; AgentTeams candidate sets are "
            "bound to the pre-simulation design fingerprint and never choose placement."
        ),
    }
    report["benchmark_fingerprint"] = canonical_sha256(report)
    return report


def build_adaptive_evidence(
    design: dict[str, Any],
    benchmark: dict[str, Any],
    agent_controller: dict[str, Any],
) -> dict[str, Any]:
    """Build a compact public receipt without raw traces or per-job records."""
    if design.get("schema_version") != DESIGN_SCHEMA or not _verified(
        design, "design_fingerprint"
    ):
        raise ValueError("Adaptive design is invalid")
    if benchmark.get("schema_version") != BENCHMARK_SCHEMA or not _verified(
        benchmark, "benchmark_fingerprint"
    ):
        raise ValueError("Adaptive benchmark is invalid")
    validate_controller_selections(agent_controller, design)
    if benchmark["design_fingerprint"] != design["design_fingerprint"]:
        raise ValueError("Benchmark and design fingerprints differ")
    if (
        benchmark["agent_controller_fingerprint"]
        != agent_controller["controller_fingerprint"]
    ):
        raise ValueError("Benchmark and AgentTeams controller fingerprints differ")
    controller_windows = {
        name: {item["window_id"]: item for item in value["windows"]}
        for name, value in benchmark["controllers"].items()
    }
    evaluation_windows: list[dict[str, Any]] = []
    for design_window in design["windows"]:
        if design_window["partition"] != "evaluation":
            continue
        window_id = str(design_window["window_id"])
        evaluation_windows.append(
            {
                "window_id": window_id,
                "date": design_window["date"],
                "regime_signals": design_window["regime_signals"],
                "controllers": {
                    name: {
                        "candidate_action_ids": value[window_id][
                            "candidate_action_ids"
                        ],
                        "selection_status": value[window_id]["selection_status"],
                        "selected_action_ids": value[window_id][
                            "selected_action_ids"
                        ],
                        "selected_allocation_rate_range": value[window_id][
                            "selected_allocation_rate_range"
                        ],
                        "selected_allocation_uplift_vs_fifo_range": value[window_id][
                            "selected_allocation_uplift_vs_fifo_range"
                        ],
                        "catalog_oracle_frontier_covered": value[window_id][
                            "catalog_oracle_frontier_covered"
                        ],
                        "catalog_oracle_frontier_exact_match": value[window_id][
                            "catalog_oracle_frontier_exact_match"
                        ],
                        "candidate_set_best_hard_pass_allocation_rate": value[
                            window_id
                        ]["candidate_set_best_hard_pass_allocation_rate"],
                        "candidate_set_allocation_regret_vs_catalog_best": value[
                            window_id
                        ]["candidate_set_allocation_regret_vs_catalog_best"],
                        "candidate_set_catalog_best_allocation_covered": value[
                            window_id
                        ][
                            "candidate_set_catalog_best_allocation_covered"
                        ],
                    }
                    for name, value in controller_windows.items()
                },
            }
        )
    source = design["trace"]["source"]
    receipt: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA,
        "study": {
            "dataset": source.get("dataset"),
            "dataset_commit": source.get("commit"),
            "source_hashes": {
                key: value
                for key, value in source.items()
                if key.endswith("_sha256")
            },
            "capacity_gpus": design["trace"]["capacity_gpus"],
            "gpu_model": design["trace"]["gpu_model"],
            "split": design["split"],
            "action_space": design["action_space"],
            "agentteams": {
                "model_id": agent_controller["model_id"],
                "controller_id": agent_controller["controller_id"],
                "provenance": agent_controller.get("provenance"),
            },
        },
        "calibration_static_selection": benchmark[
            "calibration_static_selection"
        ],
        "best_static_action_id": benchmark["best_static_action_id"],
        "controllers": {
            name: {key: value for key, value in controller.items() if key != "windows"}
            for name, controller in benchmark["controllers"].items()
        },
        "evaluation_windows": evaluation_windows,
        "limitations": [
            "This is historical offline counterfactual evaluation, not online scheduling.",
            "The AgentTeams controller sees frozen evaluation-window workload summaries, so future demand is not hidden as it would be in rolling-horizon operation.",
            "The catalog oracle uses all five simulated actions and is an offline upper bound, not a deployable controller.",
            "Conclusions apply only to the declared trace, chronological split, SLO and bounded action catalog.",
        ],
        "source_evidence": {
            "design_fingerprint": design["design_fingerprint"],
            "multiwindow_fingerprint": benchmark["multiwindow_fingerprint"],
            "rule_controller_fingerprint": benchmark[
                "rule_controller_fingerprint"
            ],
            "agent_controller_fingerprint": benchmark[
                "agent_controller_fingerprint"
            ],
            "benchmark_fingerprint": benchmark["benchmark_fingerprint"],
        },
    }
    receipt["receipt_fingerprint"] = canonical_sha256(receipt)
    return receipt
