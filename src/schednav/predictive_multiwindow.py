"""Contracts and aggregation for cutoff-safe predictive multi-window studies."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path, PurePosixPath
from statistics import median
from typing import Any

from .contracts import canonical_sha256


STUDY_SCHEMA = "schednav.predictive-multiwindow-study/v1"
RUN_RECEIPT_SCHEMA = "schednav.predictive-multiwindow-run/v1"
WINDOW_RECORD_SCHEMA = "schednav.predictive-multiwindow-record/v1"
PARTITION_SUMMARY_SCHEMA = "schednav.predictive-multiwindow-summary/v1"
SELECTION_LOCK_SCHEMA = "schednav.predictive-selection-lock/v1"
EVIDENCE_SCHEMA = "schednav.predictive-multiwindow-evidence/v1"
PARTITIONS = ("calibration", "holdout")


def _without(value: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: item for name, item in value.items() if name != key}


def _verified(value: dict[str, Any], fingerprint_key: str) -> bool:
    supplied = value.get(fingerprint_key)
    return isinstance(supplied, str) and canonical_sha256(
        _without(value, fingerprint_key)
    ) == supplied


def _project_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty project-relative path")
    raw = value
    path = PurePosixPath(raw.replace("\\", "/"))
    if (
        path == PurePosixPath(".")
        or path.is_absolute()
        or ".." in path.parts
        or ":" in path.parts[0]
    ):
        raise ValueError(f"{field} must be a safe project-relative path")
    return path.as_posix()


def load_predictive_multiwindow_study(path: Path) -> dict[str, Any]:
    """Load and semantically validate a frozen predictive study design."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != STUDY_SCHEMA:
        raise ValueError(f"Expected schema_version={STUDY_SCHEMA}")
    if not _verified(value, "design_fingerprint"):
        raise ValueError("Predictive multi-window design fingerprint is invalid")

    dataset = value.get("dataset")
    trace_contract = value.get("trace_contract")
    split = value.get("split")
    execution = value.get("execution")
    arms = value.get("arms")
    windows = value.get("windows")
    if not all(isinstance(item, dict) for item in (dataset, trace_contract, split, execution)):
        raise ValueError("Study dataset, trace, split, and execution contracts are required")
    if not isinstance(arms, list) or not 3 <= len(arms) <= 5:
        raise ValueError("Predictive studies require three to five bounded arms")
    if not isinstance(windows, list) or len(windows) < 2:
        raise ValueError("Predictive studies require at least two windows")
    if trace_contract.get("schema_version") != "schednav.trace/v2":
        raise ValueError("Tenant predictive studies require schednav.trace/v2")
    if trace_contract.get("include_warmup_spot") is not False:
        raise ValueError("The frozen study excludes pre-evaluation Spot arrivals")
    if execution.get("future_arrivals_visible_to_controllers") is not False:
        raise ValueError("Predictive controllers cannot observe future arrivals")
    if int(execution.get("repetitions_per_arm_per_window", 0)) < 2:
        raise ValueError("At least two repetitions are required")
    _project_path(execution.get("slo"), "execution.slo")

    arm_ids: list[str] = []
    baseline_count = 0
    for arm in arms:
        if not isinstance(arm, dict):
            raise ValueError("Every arm must be an object")
        arm_id = str(arm.get("arm_id", ""))
        if not arm_id or arm_id in arm_ids:
            raise ValueError("Arm IDs must be non-empty and unique")
        arm_ids.append(arm_id)
        kind = arm.get("kind")
        controller = arm.get("controller")
        if kind not in {"static", "predictive"}:
            raise ValueError("Arm kind must be static or predictive")
        if (kind == "static") != (controller is None):
            raise ValueError("Static arms omit controllers; predictive arms require one")
        _project_path(arm.get("policy"), f"arms.{arm_id}.policy")
        if controller is not None:
            _project_path(controller, f"arms.{arm_id}.controller")
        if arm_id == "fifo":
            baseline_count += 1
    if baseline_count != 1:
        raise ValueError("The frozen arm catalog requires exactly one fifo baseline")

    origin = datetime.fromisoformat(str(dataset.get("time_origin")))
    expected_window = int(trace_contract.get("evaluation_window_seconds", 0))
    minimum_history = int(execution.get("minimum_tenant_training_hours", 0)) * 3600
    window_ids: set[str] = set()
    dates: set[str] = set()
    previous_start = -1.0
    partitions: list[str] = []
    for window in windows:
        if not isinstance(window, dict):
            raise ValueError("Every study window must be an object")
        window_id = str(window.get("window_id", ""))
        date = str(window.get("date", ""))
        partition = str(window.get("partition", ""))
        start = float(window.get("start_seconds", -1))
        end = float(window.get("end_seconds", -1))
        if not window_id or window_id in window_ids or date in dates:
            raise ValueError("Window IDs and dates must be non-empty and unique")
        if partition not in PARTITIONS:
            raise ValueError("Window partition must be calibration or holdout")
        if start <= previous_start or end - start + 1 != expected_window:
            raise ValueError("Windows must be chronological and match the declared size")
        if start < minimum_history:
            raise ValueError("A selected window lacks the declared tenant training history")
        expected_date = (origin + timedelta(seconds=start)).date().isoformat()
        if date != expected_date:
            raise ValueError("Window date does not match its origin-relative start")
        window_ids.add(window_id)
        dates.add(date)
        partitions.append(partition)
        previous_start = start
    calibration_count = partitions.count("calibration")
    holdout_count = partitions.count("holdout")
    if calibration_count != int(split.get("calibration_window_count", -1)):
        raise ValueError("Calibration window count differs from the frozen split")
    if holdout_count != int(split.get("holdout_window_count", -1)):
        raise ValueError("Holdout window count differs from the frozen split")
    if partitions != ["calibration"] * calibration_count + ["holdout"] * holdout_count:
        raise ValueError("The chronological prefix must contain calibration windows first")
    if split.get("holdout_results_absent_when_selection_is_frozen") is not True:
        raise ValueError("The study must lock selection before holdout results exist")
    return value


def metric_snapshot(metrics: dict[str, Any]) -> dict[str, Any]:
    """Extract only the declared SLO/ranking fields from a metrics report."""

    return {
        "allocation_rate_mean": float(metrics["cluster"]["allocation_rate_mean"]),
        "hp_completion_rate": float(metrics["jobs"]["HP"]["completion_rate"]),
        "hp_preempted_job_count": int(metrics["jobs"]["HP"]["preempted_job_count"]),
        "hp_jct_p95_seconds": float(metrics["jobs"]["HP"]["jct_seconds"]["p95"]),
        "hp_queue_p95_seconds": float(metrics["jobs"]["HP"]["queue_seconds"]["p95"]),
        "spot_completion_rate": float(metrics["jobs"]["Spot"]["completion_rate"]),
        "spot_jct_p95_seconds": float(metrics["jobs"]["Spot"]["jct_seconds"]["p95"]),
        "spot_eviction_rate_per_run": float(
            metrics["preemption_events"]["eviction_rate_per_run"]
        ),
        "spot_guarantee_success_rate": float(metrics["spot_guarantee"]["success_rate"]),
    }


def build_arm_record(
    arm: dict[str, Any],
    receipts: list[dict[str, Any]],
    metrics: dict[str, Any],
    audit: dict[str, Any],
) -> dict[str, Any]:
    """Build one compact, deterministic arm record after repeated execution."""

    if len(receipts) < 2:
        raise ValueError("Arm records require at least two repetitions")
    if any(not _verified(item, "run_receipt_fingerprint") for item in receipts):
        raise ValueError("A run receipt fingerprint is invalid")
    repetitions = [item.get("repetition") for item in receipts]
    if any(not isinstance(item, int) or isinstance(item, bool) for item in repetitions) or (
        sorted(repetitions) != list(range(1, len(receipts) + 1))
    ):
        raise ValueError("Run receipts must contain each declared repetition exactly once")
    for item in receipts:
        if item.get("arm_id") != arm["arm_id"] or item.get("kind") != arm["kind"]:
            raise ValueError("A run receipt differs from the frozen arm")
        if item.get("future_arrivals_visible_to_controller") is not False:
            raise ValueError("A run receipt does not preserve the future-arrival boundary")
    result_fingerprints = {item.get("result_fingerprint") for item in receipts}
    metrics_fingerprints = {item.get("metrics_fingerprint") for item in receipts}
    deterministic = len(result_fingerprints) == 1 and len(metrics_fingerprints) == 1
    if not deterministic:
        raise ValueError(f"Determinism check failed for arm {arm['arm_id']}")
    if metrics.get("metrics_fingerprint") not in metrics_fingerprints:
        raise ValueError("Primary metrics do not match the repeated run receipts")
    if metrics.get("evidence", {}).get("simulation_result_fingerprint") not in (
        result_fingerprints
    ):
        raise ValueError("Primary metrics do not reference the repeated simulation result")
    if any(
        item.get("policy_fingerprint") != metrics.get("policy_fingerprint")
        for item in receipts
    ):
        raise ValueError("Run receipt and metrics policy fingerprints differ")
    if not _verified(audit, "audit_fingerprint"):
        raise ValueError("SLO audit fingerprint is invalid")
    if audit.get("metrics_fingerprint") != metrics.get("metrics_fingerprint"):
        raise ValueError("SLO audit and primary metrics do not match")
    if audit.get("policy_fingerprint") != metrics.get("policy_fingerprint"):
        raise ValueError("SLO audit and primary policy fingerprints do not match")
    failed = [
        item["id"]
        for item in audit.get("results", [])
        if item.get("severity") == "hard" and item.get("passed") is not True
    ]
    value: dict[str, Any] = {
        "arm_id": arm["arm_id"],
        "kind": arm["kind"],
        "role": arm["role"],
        "policy_fingerprint": metrics["policy_fingerprint"],
        "controller_fingerprint": (
            metrics.get("predictive_control", {}).get("controller_fingerprint")
            if arm["kind"] == "predictive"
            else None
        ),
        "result_fingerprint": receipts[0]["result_fingerprint"],
        "metrics_fingerprint": metrics["metrics_fingerprint"],
        "audit_fingerprint": audit["audit_fingerprint"],
        "repetition_count": len(receipts),
        "deterministic_repetitions": True,
        "hard_slo_passed": audit.get("audit_passed") is True,
        "failed_hard_constraints": failed,
        "metrics": metric_snapshot(metrics),
    }
    value["arm_record_fingerprint"] = canonical_sha256(value)
    return value


def build_window_record(
    study: dict[str, Any],
    window: dict[str, Any],
    trace_fingerprint: str,
    workload: dict[str, Any],
    arms: list[dict[str, Any]],
) -> dict[str, Any]:
    if [item["arm_id"] for item in arms] != [item["arm_id"] for item in study["arms"]]:
        raise ValueError("Window arm order differs from the frozen catalog")
    value: dict[str, Any] = {
        "schema_version": WINDOW_RECORD_SCHEMA,
        "design_fingerprint": study["design_fingerprint"],
        "window_id": window["window_id"],
        "date": window["date"],
        "partition": window["partition"],
        "window_seconds": {
            "start": window["start_seconds"],
            "end": window["end_seconds"],
        },
        "stratum": window["stratum"],
        "trace_fingerprint": trace_fingerprint,
        "workload_fingerprint": workload["workload_fingerprint"],
        "population": {
            job_type: int(workload["population"][job_type]["job_count"])
            for job_type in ("HP", "Spot")
        },
        "regime_signals": workload["regime_signals"],
        "arms": arms,
    }
    value["window_record_fingerprint"] = canonical_sha256(value)
    return value


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        raise ValueError("At least one value is required")
    return {
        "mean": round(sum(values) / len(values), 9),
        "median": round(float(median(values)), 9),
        "min": round(min(values), 9),
        "max": round(max(values), 9),
    }


def _calibration_selection(
    arm_summaries: dict[str, dict[str, Any]],
    *,
    window_count: int,
    allocation_tie_band: float,
) -> dict[str, Any]:
    eligible = [
        arm_id
        for arm_id, summary in arm_summaries.items()
        if summary["hard_slo_pass_count"] == window_count
        and summary["deterministic_window_count"] == window_count
    ]
    stages: list[dict[str, Any]] = [
        {"stage": "all_window_hard_slo_filter", "remaining_arm_ids": eligible}
    ]
    remaining = eligible
    if remaining:
        best = max(
            float(arm_summaries[arm_id]["allocation_rate_mean"]["mean"])
            for arm_id in remaining
        )
        remaining = [
            arm_id
            for arm_id in remaining
            if best - float(arm_summaries[arm_id]["allocation_rate_mean"]["mean"])
            < allocation_tie_band
        ]
        stages.append(
            {
                "stage": "maximize_mean_allocation_rate",
                "best_observed": best,
                "strict_tie_band": allocation_tie_band,
                "remaining_arm_ids": remaining,
            }
        )
    if len(remaining) > 1:
        best = min(
            float(arm_summaries[arm_id]["spot_jct_p95_seconds"]["mean"])
            for arm_id in remaining
        )
        remaining = [
            arm_id
            for arm_id in remaining
            if float(arm_summaries[arm_id]["spot_jct_p95_seconds"]["mean"])
            == best
        ]
        stages.append(
            {
                "stage": "minimize_mean_spot_p95_jct",
                "best_observed": best,
                "remaining_arm_ids": remaining,
            }
        )
    if len(remaining) > 1:
        best = min(
            float(arm_summaries[arm_id]["spot_eviction_rate_per_run"]["mean"])
            for arm_id in remaining
        )
        remaining = [
            arm_id
            for arm_id in remaining
            if float(arm_summaries[arm_id]["spot_eviction_rate_per_run"]["mean"])
            == best
        ]
        stages.append(
            {
                "stage": "minimize_mean_spot_eviction_rate_per_run",
                "best_observed": best,
                "remaining_arm_ids": remaining,
            }
        )
    status = (
        "no_eligible_arm"
        if not remaining
        else "selected"
        if len(remaining) == 1
        else "tie_requires_human_approval"
    )
    value: dict[str, Any] = {
        "status": status,
        "selected_arm_ids": remaining,
        "stages": stages,
        "weighted_score_used": False,
        "definition": "Arms must pass every hard SLO in every calibration window. Eligible arms follow the declared allocation, Spot JCT, and eviction hierarchy; unresolved ties remain parallel.",
    }
    value["selection_fingerprint"] = canonical_sha256(value)
    return value


def build_partition_summary(
    study: dict[str, Any],
    partition: str,
    records: list[dict[str, Any]],
    *,
    allocation_tie_band: float,
) -> dict[str, Any]:
    """Aggregate one chronological partition without a weighted score."""

    if partition not in PARTITIONS:
        raise ValueError("partition must be calibration or holdout")
    expected = [item for item in study["windows"] if item["partition"] == partition]
    if [item["window_id"] for item in records] != [item["window_id"] for item in expected]:
        raise ValueError("Partition records differ from the frozen window order")
    if any(not _verified(item, "window_record_fingerprint") for item in records):
        raise ValueError("A window record fingerprint is invalid")

    arm_ids = [item["arm_id"] for item in study["arms"]]
    by_arm = {arm_id: [] for arm_id in arm_ids}
    for record in records:
        if [item["arm_id"] for item in record["arms"]] != arm_ids:
            raise ValueError("A window record differs from the frozen arm catalog")
        for arm in record["arms"]:
            by_arm[arm["arm_id"]].append(arm)
    baseline = by_arm["fifo"]
    summaries: dict[str, Any] = {}
    for arm_id in arm_ids:
        values = by_arm[arm_id]
        allocation = [float(item["metrics"]["allocation_rate_mean"]) for item in values]
        deltas = [
            current - float(base["metrics"]["allocation_rate_mean"])
            for current, base in zip(allocation, baseline)
        ]
        failures: dict[str, int] = {}
        for item in values:
            for constraint in item["failed_hard_constraints"]:
                failures[constraint] = failures.get(constraint, 0) + 1
        summaries[arm_id] = {
            "window_count": len(values),
            "deterministic_window_count": sum(
                item["deterministic_repetitions"] is True for item in values
            ),
            "hard_slo_pass_count": sum(item["hard_slo_passed"] is True for item in values),
            "hard_slo_pass_rate": round(
                sum(item["hard_slo_passed"] is True for item in values) / len(values), 9
            ),
            "failed_hard_constraint_window_counts": dict(sorted(failures.items())),
            "allocation_rate_mean": _stats(allocation),
            "allocation_delta_vs_fifo": {
                **_stats(deltas),
                "positive_window_count": sum(item > 0 for item in deltas),
                "equal_window_count": sum(item == 0 for item in deltas),
                "negative_window_count": sum(item < 0 for item in deltas),
            },
            "hp_jct_p95_seconds": _stats(
                [float(item["metrics"]["hp_jct_p95_seconds"]) for item in values]
            ),
            "spot_jct_p95_seconds": _stats(
                [float(item["metrics"]["spot_jct_p95_seconds"]) for item in values]
            ),
            "spot_eviction_rate_per_run": _stats(
                [float(item["metrics"]["spot_eviction_rate_per_run"]) for item in values]
            ),
            "spot_guarantee_success_rate": _stats(
                [float(item["metrics"]["spot_guarantee_success_rate"]) for item in values]
            ),
        }
    value: dict[str, Any] = {
        "schema_version": PARTITION_SUMMARY_SCHEMA,
        "design_fingerprint": study["design_fingerprint"],
        "partition": partition,
        "window_count": len(records),
        "baseline_arm_id": "fifo",
        "arms": summaries,
        "windows": records,
        "definition": "All metrics are same-trace, same-window counterfactual results. Hard-SLO pass counts and the declared hierarchy are reported separately; no cross-metric weighted score is constructed.",
    }
    if partition == "calibration":
        value["calibration_selection"] = _calibration_selection(
            summaries,
            window_count=len(records),
            allocation_tie_band=allocation_tie_band,
        )
    value["partition_summary_fingerprint"] = canonical_sha256(value)
    return value


def build_selection_lock(
    study: dict[str, Any], calibration_summary: dict[str, Any]
) -> dict[str, Any]:
    """Freeze calibration output before any holdout simulation artifacts exist."""

    if calibration_summary.get("partition") != "calibration" or not _verified(
        calibration_summary, "partition_summary_fingerprint"
    ):
        raise ValueError("A valid calibration summary is required")
    if calibration_summary.get("design_fingerprint") != study["design_fingerprint"]:
        raise ValueError("Calibration summary and study fingerprints differ")
    selection = calibration_summary["calibration_selection"]
    value: dict[str, Any] = {
        "schema_version": SELECTION_LOCK_SCHEMA,
        "design_fingerprint": study["design_fingerprint"],
        "calibration_summary_fingerprint": calibration_summary[
            "partition_summary_fingerprint"
        ],
        "calibration_selection_fingerprint": selection["selection_fingerprint"],
        "selection_status": selection["status"],
        "selected_arm_ids": selection["selected_arm_ids"],
        "holdout_result_count_at_freeze": 0,
        "holdout_results_absent_at_freeze": True,
        "human_approval_required": selection["status"] == "tie_requires_human_approval",
        "definition": "This content-addressed lock is created only while every holdout run directory is absent. Later holdout execution must present the matching lock fingerprint.",
    }
    value["selection_lock_fingerprint"] = canonical_sha256(value)
    return value


def verify_selection_lock(study: dict[str, Any], value: dict[str, Any]) -> None:
    if value.get("schema_version") != SELECTION_LOCK_SCHEMA or not _verified(
        value, "selection_lock_fingerprint"
    ):
        raise ValueError("Selection lock is invalid")
    if value.get("design_fingerprint") != study["design_fingerprint"]:
        raise ValueError("Selection lock and study fingerprints differ")
    if value.get("holdout_result_count_at_freeze") != 0 or value.get(
        "holdout_results_absent_at_freeze"
    ) is not True:
        raise ValueError("Selection lock does not prove an empty holdout result boundary")


def build_public_evidence(
    study: dict[str, Any],
    calibration: dict[str, Any],
    selection_lock: dict[str, Any],
    holdout: dict[str, Any],
) -> dict[str, Any]:
    """Build a compact public receipt without raw traces or per-job results."""

    verify_selection_lock(study, selection_lock)
    for summary, partition in ((calibration, "calibration"), (holdout, "holdout")):
        if summary.get("partition") != partition or not _verified(
            summary, "partition_summary_fingerprint"
        ):
            raise ValueError(f"A valid {partition} summary is required")
        if summary.get("design_fingerprint") != study["design_fingerprint"]:
            raise ValueError("Partition summary and study fingerprints differ")
    if selection_lock["calibration_summary_fingerprint"] != calibration[
        "partition_summary_fingerprint"
    ]:
        raise ValueError("Selection lock does not match the calibration summary")

    selected = list(selection_lock["selected_arm_ids"])
    selected_holdout = {
        arm_id: holdout["arms"][arm_id]
        for arm_id in selected
    }
    if not selected:
        status = "no_calibration_eligible_arm"
    elif all(
        item["hard_slo_pass_count"] == holdout["window_count"]
        for item in selected_holdout.values()
    ):
        status = (
            "holdout_passed"
            if len(selected) == 1
            else "holdout_tie_requires_human_approval"
        )
    else:
        status = "holdout_rejected"

    compact_windows = []
    for summary in (calibration, holdout):
        for window in summary["windows"]:
            compact_windows.append(
                {
                    "window_id": window["window_id"],
                    "date": window["date"],
                    "partition": window["partition"],
                    "trace_fingerprint": window["trace_fingerprint"],
                    "population": window["population"],
                    "regime_signals": window["regime_signals"],
                    "arms": {
                        arm["arm_id"]: {
                            "hard_slo_passed": arm["hard_slo_passed"],
                            "failed_hard_constraints": arm["failed_hard_constraints"],
                            "deterministic_repetitions": arm[
                                "deterministic_repetitions"
                            ],
                            "metrics_fingerprint": arm["metrics_fingerprint"],
                            "metrics": arm["metrics"],
                        }
                        for arm in window["arms"]
                    },
                }
            )
    value: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA,
        "status": status,
        "study": {
            "study_id": study["study_id"],
            "design_fingerprint": study["design_fingerprint"],
            "dataset": study["dataset"],
            "prior_selection_fingerprint": study["prior_window_selection"][
                "selection_fingerprint"
            ],
            "trace_contract": study["trace_contract"],
            "split": study["split"],
            "repetitions_per_arm_per_window": study["execution"][
                "repetitions_per_arm_per_window"
            ],
            "arms": study["arms"],
        },
        "information_boundary": {
            "window_selection_precedes_predictive_simulation": True,
            "controller_future_arrivals_visible": False,
            "selection_lock_precedes_holdout_simulation": True,
            "selection_lock_fingerprint": selection_lock[
                "selection_lock_fingerprint"
            ],
        },
        "calibration": {
            "summary_fingerprint": calibration["partition_summary_fingerprint"],
            "selection": calibration["calibration_selection"],
            "arms": calibration["arms"],
        },
        "holdout": {
            "summary_fingerprint": holdout["partition_summary_fingerprint"],
            "selected_arm_ids_frozen_before_execution": selected,
            "selected_arms": selected_holdout,
            "arms": holdout["arms"],
        },
        "windows": compact_windows,
        "interpretation": {
            "claim_scope": "historical cutoff-safe multi-window shadow replay",
            "performance_claim": (
                "Only arms that remain hard-SLO compliant on every hidden holdout window may be described as holdout-passed; allocation and latency differences are reported directly rather than converted to an LLM-weighted score."
            ),
        },
        "limitations": [
            "This is historical shadow replay, not a live-cluster deployment.",
            "The controller sees only state and arrivals available at each cutoff, but simulator outcomes still depend on the fidelity of the first-party cluster model.",
            "The 11 windows come from one GPU model in one source trace and do not establish universal cross-cluster superiority.",
            "Raw source rows, canonical per-job traces, checkpoints, and per-job simulation results are not redistributed.",
        ],
    }
    value["receipt_fingerprint"] = canonical_sha256(value)
    return value
