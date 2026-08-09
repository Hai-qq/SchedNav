"""Publish a compact, content-addressed tenant-predictive control receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT_DEFAULT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from schednav.contracts import canonical_sha256
from schednav.native_trace import load_canonical_trace


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _validate_fingerprint(value: dict[str, Any], field: str) -> None:
    supplied = value.get(field)
    payload = {key: item for key, item in value.items() if key != field}
    if not isinstance(supplied, str) or canonical_sha256(payload) != supplied:
        raise ValueError(f"Invalid {field}: {supplied}")


def _require_repeat(first: dict[str, Any], second: dict[str, Any], name: str) -> None:
    if first != second:
        raise ValueError(f"Repeated {name} artifacts are not byte-equivalent JSON")


def build_receipt(
    trace_path: Path,
    forecast_paths: tuple[Path, Path],
    result_paths: tuple[Path, Path],
    metrics_paths: tuple[Path, Path],
    baseline_path: Path,
    audit_path: Path,
    bridge_task_paths: tuple[Path, Path],
    bridge_forecast_path: Path,
    bridge_metrics_path: Path,
) -> dict[str, Any]:
    trace = load_canonical_trace(trace_path)
    forecasts = tuple(_load(path) for path in forecast_paths)
    results = tuple(_load(path) for path in result_paths)
    metrics_values = tuple(_load(path) for path in metrics_paths)
    baseline = _load(baseline_path)
    audit = _load(audit_path)
    bridge_tasks = tuple(_load(path) for path in bridge_task_paths)
    bridge_forecast = _load(bridge_forecast_path)
    bridge_metrics = _load(bridge_metrics_path)

    _require_repeat(forecasts[0], forecasts[1], "forecast")
    _require_repeat(results[0], results[1], "simulation")
    _require_repeat(metrics_values[0], metrics_values[1], "metrics")
    _require_repeat(forecasts[0], bridge_forecast, "bridge forecast")
    _require_repeat(metrics_values[0], bridge_metrics, "bridge metrics")
    for value in forecasts:
        _validate_fingerprint(value, "observation_bundle_fingerprint")
    for value in results:
        _validate_fingerprint(value, "result_fingerprint")
    for value in (*metrics_values, baseline):
        _validate_fingerprint(value, "metrics_fingerprint")
    _validate_fingerprint(audit, "audit_fingerprint")

    forecast = forecasts[0]
    result = results[0]
    metrics = metrics_values[0]
    control = result["predictive_control"]
    if trace.schema_version != "schednav.trace/v2":
        raise ValueError("Tenant-predictive evidence requires schednav.trace/v2")
    if metrics["source"]["trace_fingerprint"] != trace.fingerprint:
        raise ValueError("Metrics and Trace fingerprints do not match")
    if baseline["source"]["trace_fingerprint"] != trace.fingerprint:
        raise ValueError("FIFO baseline and Trace fingerprints do not match")
    if audit["metrics_fingerprint"] != metrics["metrics_fingerprint"]:
        raise ValueError("Audit and metrics fingerprints do not match")
    if audit["baseline"]["metrics_fingerprint"] != baseline["metrics_fingerprint"]:
        raise ValueError("Audit and FIFO baseline fingerprints do not match")
    if forecast["information_boundary"]["actual_future_demand_used_for_prediction"]:
        raise ValueError("Forecast artifact crosses the declared information boundary")
    if control["information_boundary"]["actual_future_demand_used_for_prediction"]:
        raise ValueError("Control artifact crosses the declared information boundary")
    expected_bridge_operations = ("forecast_demand", "simulate_predictive_policy")
    for task, operation in zip(bridge_tasks, expected_bridge_operations, strict=True):
        if task.get("operation") != operation or task.get("status") != "succeeded":
            raise ValueError(f"Bridge task did not succeed as {operation}")

    hard_results = [
        item for item in audit["results"] if item["severity"] == "hard"
    ]
    failed_hard = [item["id"] for item in hard_results if not item["passed"]]
    hp = metrics["jobs"]["HP"]
    spot = metrics["jobs"]["Spot"]
    feedback = control["feedback_events"]
    receipt: dict[str, Any] = {
        "schema_version": "schednav.predictive-evidence/v1",
        "status": "hard_slo_rejected" if failed_hard else "hard_slo_passed",
        "dataset": {
            "name": trace.source.get("dataset"),
            "repository": trace.source.get("repository"),
            "commit": trace.source.get("commit"),
            "node_info_sha256": trace.source.get("node_info_sha256"),
            "job_info_sha256": trace.source.get("job_info_sha256"),
        },
        "trace": {
            "schema_version": trace.schema_version,
            "trace_id": trace.trace_id,
            "trace_fingerprint": trace.fingerprint,
            "node_count": len(trace.nodes),
            "capacity_gpus": trace.capacity_gpus,
            "replayed_arrival_count": len(trace.jobs),
            "tenant_count": len({job.tenant_id for job in trace.jobs}),
            "evaluation_window_seconds": {
                "start": trace.evaluation_start_seconds,
                "end": trace.evaluation_end_seconds,
            },
            "evaluation_population": {
                "HP": hp["job_count"],
                "Spot": spot["job_count"],
            },
            "warmup_spot_included": trace.source["filter"]["include_warmup_spot"],
            "tenant_mapping": trace.source["filter"]["tenant_mapping"],
        },
        "controller": {
            "controller_id": control["controller_id"],
            "controller_fingerprint": control["controller_fingerprint"],
            "model": control["model"]["model"],
            "cadence_seconds": {
                "demand_sample": control["controller"][
                    "demand_sample_interval_seconds"
                ],
                "quota_update": control["controller"][
                    "quota_update_interval_seconds"
                ],
                "retrain": control["controller"]["retrain_interval_seconds"],
            },
            "lookback_hours": control["controller"]["lookback_hours"],
            "forecast_horizon_hours": control["controller"][
                "forecast_horizon_hours"
            ],
            "guarantee_probability": control["controller"][
                "guarantee_probability"
            ],
            "tenant_and_resource_pool_scoped": True,
        },
        "cutoff_forecast": {
            "cutoff_time_seconds": forecast["cutoff_time_seconds"],
            "observation_bundle_fingerprint": forecast[
                "observation_bundle_fingerprint"
            ],
            "model_fingerprint": forecast["demand_forecast"]["model"][
                "model_fingerprint"
            ],
            "series_count": forecast["demand_forecast"]["model"]["series_count"],
            "training": forecast["demand_forecast"]["model"]["training"],
            "points": forecast["demand_forecast"]["points"],
            "quota_by_guarantee_hour": forecast["spot_quota_plan"][
                "spot_quota_gpus_by_guarantee_hour"
            ],
            "future_demand_used": False,
            "deterministic_repeat": True,
        },
        "closed_loop_replay": {
            "action_id": metrics["policy"]["action_id"],
            "policy_fingerprint": metrics["policy_fingerprint"],
            "result_fingerprint": result["result_fingerprint"],
            "metrics_fingerprint": metrics["metrics_fingerprint"],
            "control_fingerprint": control["control_fingerprint"],
            "model_fingerprint": control["model"]["model_fingerprint"],
            "deterministic_repeat": True,
            "update_count": control["update_count"],
            "demand_sample_count": control["demand_sample_count"],
            "quota_minimum_gpus": control["spot_quota_gpus"]["minimum"],
            "quota_maximum_gpus": control["spot_quota_gpus"]["maximum"],
            "eta_final_by_resource_pool": control["eta"][
                "final_by_resource_pool"
            ],
            "feedback_events_by_resource_pool": feedback,
            "forecast_evaluation": control["forecast_evaluation"],
            "metrics": {
                "hp_completion_rate": hp["completion_rate"],
                "hp_preempted_job_count": hp["preempted_job_count"],
                "hp_p95_jct_seconds": hp["jct_seconds"]["p95"],
                "hp_p95_queue_seconds": hp["queue_seconds"]["p95"],
                "spot_completion_rate": spot["completion_rate"],
                "spot_p95_jct_seconds": spot["jct_seconds"]["p95"],
                "spot_eviction_rate_per_run": metrics["preemption_events"][
                    "eviction_rate_per_run"
                ],
                "spot_guarantee_success_rate": metrics["spot_guarantee"][
                    "success_rate"
                ],
                "allocation_rate_mean": metrics["cluster"][
                    "allocation_rate_mean"
                ],
            },
        },
        "fifo_baseline": {
            "metrics_fingerprint": baseline["metrics_fingerprint"],
            "allocation_rate_mean": baseline["cluster"]["allocation_rate_mean"],
            "hp_p95_jct_seconds": baseline["jobs"]["HP"]["jct_seconds"]["p95"],
        },
        "slo_audit": {
            "slo_fingerprint": audit["slo_fingerprint"],
            "audit_fingerprint": audit["audit_fingerprint"],
            "audit_passed": audit["audit_passed"],
            "hard_constraint_count": len(hard_results),
            "hard_pass_count": len(hard_results) - len(failed_hard),
            "failed_hard_constraints": failed_hard,
        },
        "agentteams_bridge": {
            "profile": "configs/agentteams/host-bridge-predictive-v1.json",
            "run_config_id": "tenant-predictive-local",
            "controller_id": control["controller_id"],
            "forecast": {
                "task_id": bridge_tasks[0]["task_id"],
                "request_fingerprint": bridge_tasks[0]["request_fingerprint"],
                "status": bridge_tasks[0]["status"],
                "output_matches_cli": True,
            },
            "simulation": {
                "task_id": bridge_tasks[1]["task_id"],
                "request_fingerprint": bridge_tasks[1]["request_fingerprint"],
                "status": bridge_tasks[1]["status"],
                "output_matches_cli": True,
            },
        },
        "interpretation": {
            "claim_scope": "single-window implementation evidence",
            "result": (
                "The tenant-aware forecasting and quota feedback loop is executable, "
                "cutoff-safe and deterministic on this real window. It is rejected by "
                "the declared SLO because allocation is below the compatible FIFO baseline."
            ),
        },
        "limitations": [
            "This is one historical shadow-replay window, not a production online deployment.",
            "The one-shot cutoff command reconstructs occupancy from submit time and duration; the closed-loop replay uses actual simulator queue and allocation state.",
            "The result is negative performance evidence and must not be presented as superiority over FIFO.",
            "Raw source rows, canonical per-job files, model checkpoints and per-job simulation results are not redistributed.",
        ],
    }
    receipt["receipt_fingerprint"] = canonical_sha256(receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--forecast", action="append", required=True)
    parser.add_argument("--result", action="append", required=True)
    parser.add_argument("--metrics", action="append", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--bridge-task", action="append", required=True)
    parser.add_argument("--bridge-forecast", required=True)
    parser.add_argument("--bridge-metrics", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if (
        len(args.forecast) != 2
        or len(args.result) != 2
        or len(args.metrics) != 2
        or len(args.bridge_task) != 2
    ):
        raise ValueError(
            "Exactly two forecast, result, metrics, and bridge-task artifacts are required"
        )
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite evidence: {output}")
    receipt = build_receipt(
        Path(args.trace).resolve(),
        tuple(Path(item).resolve() for item in args.forecast),
        tuple(Path(item).resolve() for item in args.result),
        tuple(Path(item).resolve() for item in args.metrics),
        Path(args.baseline).resolve(),
        Path(args.audit).resolve(),
        tuple(Path(item).resolve() for item in args.bridge_task),
        Path(args.bridge_forecast).resolve(),
        Path(args.bridge_metrics).resolve(),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "receipt_fingerprint": receipt["receipt_fingerprint"],
                "status": receipt["status"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
