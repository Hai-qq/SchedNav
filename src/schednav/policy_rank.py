"""Deterministic hierarchical selection among SLO-audited policy metrics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import canonical_sha256
from .metric_catalog import get_metric_value
from .policy_compare import ACTION_CONTROL_FIELDS


def _load_verified(path: Path, fingerprint_key: str) -> tuple[dict[str, Any], bool]:
    report = json.loads(path.read_text(encoding="utf-8"))
    supplied = report.get(fingerprint_key)
    payload = {key: value for key, value in report.items() if key != fingerprint_key}
    return report, isinstance(supplied, str) and canonical_sha256(payload) == supplied


def rank_audited_policies(
    metrics_paths: list[Path],
    audit_paths: list[Path],
    slo_path: Path,
) -> dict[str, Any]:
    if not 3 <= len(metrics_paths) <= 5 or len(metrics_paths) != len(audit_paths):
        raise ValueError("Ranking requires matching metrics/audit lists for 3 to 5 policies")
    slo = json.loads(slo_path.read_text(encoding="utf-8"))
    ranking = slo.get("ranking")
    if not isinstance(ranking, dict):
        raise ValueError("SLO spec has no hierarchical ranking policy")
    slo_fingerprint = canonical_sha256(slo)

    metrics_loaded = [_load_verified(path, "metrics_fingerprint") for path in metrics_paths]
    audit_loaded = [_load_verified(path, "audit_fingerprint") for path in audit_paths]
    metrics_by_fingerprint = {
        report.get("metrics_fingerprint"): (report, valid) for report, valid in metrics_loaded
    }

    candidates: list[dict[str, Any]] = []
    for audit, audit_valid in audit_loaded:
        metrics_fingerprint = audit.get("metrics_fingerprint")
        if metrics_fingerprint not in metrics_by_fingerprint:
            raise ValueError(f"Audit has no matching metrics report: {metrics_fingerprint}")
        metrics, metrics_valid = metrics_by_fingerprint[metrics_fingerprint]
        if audit.get("slo_fingerprint") != slo_fingerprint:
            raise ValueError("Audit and ranking SLO fingerprints differ")
        allocation = get_metric_value(metrics, ranking["allocation_metric"])
        spot_jct = get_metric_value(metrics, ranking["second_metric"])
        eviction_rate = get_metric_value(metrics, ranking["third_metric"])
        if allocation is None or spot_jct is None or eviction_rate is None:
            raise ValueError("Ranking metric is unavailable")
        soft_target = next(
            (item for item in audit.get("results", []) if item.get("id") == "allocation-soft-target"),
            None,
        )
        candidates.append(
            {
                "policy_fingerprint": metrics.get("policy_fingerprint"),
                "metrics_fingerprint": metrics_fingerprint,
                "audit_fingerprint": audit.get("audit_fingerprint"),
                "action": {
                    key: metrics.get("policy", {}).get(key)
                    for key in sorted(ACTION_CONTROL_FIELDS)
                    if key in metrics.get("policy", {})
                },
                "evidence_valid": metrics_valid and audit_valid,
                "hard_slo_passed": audit.get("audit_passed") is True,
                "allocation_soft_target_met": soft_target.get("passed") if soft_target else None,
                "allocation_rate_mean": float(allocation),
                "spot_jct_p95_seconds": float(spot_jct),
                "spot_eviction_rate_per_run": float(eviction_rate),
            }
        )

    eligible = [
        item for item in candidates if item["evidence_valid"] and item["hard_slo_passed"]
    ]
    stages: list[dict[str, Any]] = [
        {
            "stage": "hard_slo_filter",
            "remaining_policy_fingerprints": [item["policy_fingerprint"] for item in eligible],
        }
    ]
    remaining = eligible
    if remaining:
        best_allocation = max(item["allocation_rate_mean"] for item in remaining)
        tie_band = float(ranking["allocation_tie_band"])
        remaining = [
            item for item in remaining if best_allocation - item["allocation_rate_mean"] < tie_band
        ]
        stages.append(
            {
                "stage": "maximize_allocation_rate",
                "best_observed": best_allocation,
                "strict_tie_band": tie_band,
                "remaining_policy_fingerprints": [item["policy_fingerprint"] for item in remaining],
            }
        )
    if len(remaining) > 1:
        best_spot_jct = min(item["spot_jct_p95_seconds"] for item in remaining)
        remaining = [item for item in remaining if item["spot_jct_p95_seconds"] == best_spot_jct]
        stages.append(
            {
                "stage": "minimize_spot_p95_jct",
                "best_observed": best_spot_jct,
                "remaining_policy_fingerprints": [item["policy_fingerprint"] for item in remaining],
            }
        )
    if len(remaining) > 1:
        best_eviction_rate = min(item["spot_eviction_rate_per_run"] for item in remaining)
        remaining = [
            item for item in remaining
            if item["spot_eviction_rate_per_run"] == best_eviction_rate
        ]
        stages.append(
            {
                "stage": "minimize_spot_eviction_rate_per_run",
                "best_observed": best_eviction_rate,
                "remaining_policy_fingerprints": [item["policy_fingerprint"] for item in remaining],
            }
        )

    if not remaining:
        status = "no_eligible_policy"
    elif len(remaining) == 1:
        status = "selected"
    else:
        status = "tie_requires_human_approval"
    report: dict[str, Any] = {
        "schema_version": "schednav.policy-ranking/v1",
        "slo_name": slo.get("name"),
        "slo_fingerprint": slo_fingerprint,
        "selection_status": status,
        "candidates": candidates,
        "stages": stages,
        "selected_policy_fingerprints": [item["policy_fingerprint"] for item in remaining],
        "definition": "Policies first pass every hard SLO. Ranking then maximizes allocation rate; candidates within a strict one-percentage-point band use lower Spot p95 JCT, then lower evictions per run. No weighted score or unlisted tie-breaker is used.",
    }
    report["ranking_fingerprint"] = canonical_sha256(report)
    return report
