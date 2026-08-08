"""Deterministic SLO audit over canonical GFS metrics."""

from __future__ import annotations

import json
import operator
from pathlib import Path
from typing import Any, Callable

from .contracts import canonical_sha256
from .metric_catalog import METRIC_CATALOG, get_metric_value


OPERATORS: dict[str, Callable[[float, float], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


def _verified_metrics(path: Path) -> tuple[dict[str, Any], bool]:
    report = json.loads(path.read_text(encoding="utf-8"))
    supplied = report.get("metrics_fingerprint")
    payload = {key: value for key, value in report.items() if key != "metrics_fingerprint"}
    return report, isinstance(supplied, str) and canonical_sha256(payload) == supplied


def _population(report: dict[str, Any]) -> dict[str, int] | None:
    try:
        return {job_type: int(report["jobs"][job_type]["job_count"]) for job_type in ("HP", "Spot")}
    except (KeyError, TypeError, ValueError):
        return None


def _baseline_compatible(metrics: dict[str, Any], baseline: dict[str, Any]) -> bool:
    return (
        metrics.get("source") == baseline.get("source")
        and metrics.get("trace_id") == baseline.get("trace_id")
        and metrics.get("window_seconds") == baseline.get("window_seconds")
        and _population(metrics) == _population(baseline)
        and baseline.get("policy", {}).get("scheduler") == "fifo_spot"
    )


def audit_slo(
    metrics_path: Path,
    slo_path: Path,
    baseline_metrics_path: Path | None = None,
) -> dict[str, Any]:
    metrics, metrics_valid = _verified_metrics(metrics_path)
    slo = json.loads(slo_path.read_text(encoding="utf-8"))
    if slo.get("schema_version") != "schednav.slo-spec/v1":
        raise ValueError("Unsupported SLO schema")
    if not isinstance(slo.get("name"), str) or not slo["name"].strip():
        raise ValueError("SLO spec requires a non-empty name")
    constraints = slo.get("constraints")
    if not isinstance(constraints, list) or not constraints:
        raise ValueError("SLO spec requires at least one constraint")

    baseline_required = any(isinstance(item.get("threshold"), dict) for item in constraints)
    baseline: dict[str, Any] | None = None
    baseline_valid = False
    if baseline_metrics_path is not None:
        baseline, baseline_valid = _verified_metrics(baseline_metrics_path)
    baseline_compatible = baseline is not None and _baseline_compatible(metrics, baseline)

    seen_ids: set[str] = set()
    results: list[dict[str, Any]] = []
    for constraint in constraints:
        constraint_id = str(constraint["id"])
        metric_name = str(constraint["metric"])
        comparison = str(constraint["operator"])
        threshold_spec = constraint["threshold"]
        threshold_source: dict[str, Any] = {"kind": "absolute"}
        if isinstance(threshold_spec, dict):
            if threshold_spec.get("kind") != "baseline_relative":
                raise ValueError(f"Unsupported threshold kind: {threshold_spec.get('kind')}")
            baseline_metric = str(threshold_spec["baseline_metric"])
            if baseline_metric not in METRIC_CATALOG:
                raise ValueError(f"Unsupported baseline SLO metric: {baseline_metric}")
            baseline_value = get_metric_value(baseline, baseline_metric) if baseline_compatible else None
            multiplier = float(threshold_spec.get("multiplier", 1.0))
            addend = float(threshold_spec.get("addend", 0.0))
            threshold = float(baseline_value) * multiplier + addend if baseline_value is not None else None
            threshold_source = {
                "kind": "baseline_relative",
                "baseline_metric": baseline_metric,
                "baseline_observed": baseline_value,
                "multiplier": multiplier,
                "addend": addend,
            }
        else:
            threshold = float(threshold_spec)
        severity = str(constraint["severity"])
        if constraint_id in seen_ids:
            raise ValueError(f"Duplicate SLO constraint id: {constraint_id}")
        if metric_name not in METRIC_CATALOG:
            raise ValueError(f"Unsupported SLO metric: {metric_name}")
        if comparison not in OPERATORS:
            raise ValueError(f"Unsupported SLO operator: {comparison}")
        if severity not in {"hard", "soft"}:
            raise ValueError(f"Unsupported SLO severity: {severity}")
        seen_ids.add(constraint_id)
        observed = get_metric_value(metrics, metric_name)
        passed = (
            observed is not None
            and threshold is not None
            and OPERATORS[comparison](float(observed), threshold)
        )
        results.append(
            {
                "id": constraint_id,
                "metric": metric_name,
                "operator": comparison,
                "threshold": threshold,
                "threshold_source": threshold_source,
                "severity": severity,
                "observed": observed,
                "status": "passed" if passed else (
                    "unavailable" if observed is None or threshold is None else "failed"
                ),
                "passed": passed,
            }
        )

    hard_results = [item for item in results if item["severity"] == "hard"]
    metrics_schema_supported = metrics.get("schema_version") == "schednav.metrics-report/v1"
    evidence_checks = {
        "preemption_ledger_consistent": metrics.get("preemption_events", {}).get("available") is True
        and metrics.get("preemption_events", {}).get("consistent_with_job_csv") is True,
        "spot_run_ledger_consistent": metrics.get("spot_runs", {}).get("available") is True
        and metrics.get("spot_runs", {}).get("consistent_with_job_csv") is True,
        "spot_guarantee_ledger_consistent": metrics.get("spot_guarantee", {}).get("available") is True
        and metrics.get("spot_guarantee", {}).get("consistent_with_preemption_events") is True,
        "baseline_required": baseline_required,
        "baseline_metrics_fingerprint_valid": baseline_valid if baseline_required else True,
        "baseline_compatible_fifo": baseline_compatible if baseline_required else True,
    }
    report: dict[str, Any] = {
        "schema_version": "schednav.slo-audit/v1",
        "slo_name": slo["name"],
        "slo_fingerprint": canonical_sha256(slo),
        "metrics_fingerprint": metrics.get("metrics_fingerprint"),
        "metrics_schema_supported": metrics_schema_supported,
        "metrics_fingerprint_valid": metrics_valid,
        "trace_id": metrics.get("trace_id"),
        "policy_fingerprint": metrics.get("policy_fingerprint"),
        "scheduler": metrics.get("policy", {}).get("scheduler"),
        "baseline": {
            "required": baseline_required,
            "metrics_fingerprint": baseline.get("metrics_fingerprint") if baseline is not None else None,
            "policy_fingerprint": baseline.get("policy_fingerprint") if baseline is not None else None,
            "compatible_fifo": baseline_compatible,
        },
        "evidence_checks": evidence_checks,
        "audit_passed": metrics_schema_supported
        and metrics_valid
        and all(value for key, value in evidence_checks.items() if key != "baseline_required")
        and all(item["passed"] for item in hard_results),
        "hard_constraint_count": len(hard_results),
        "soft_violation_count": sum(
            item["severity"] == "soft" and not item["passed"] for item in results
        ),
        "results": results,
        "definition": "Hard constraints and required evidence/baseline checks determine audit_passed. Soft failures remain visible and are consumed only by the declared hierarchical ranking policy.",
    }
    report["audit_fingerprint"] = canonical_sha256(report)
    return report
