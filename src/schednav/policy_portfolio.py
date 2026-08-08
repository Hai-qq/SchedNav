"""Neutral comparison of three to five canonical policy metrics reports."""

from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path
from typing import Any

from .contracts import canonical_sha256
from .metric_catalog import METRIC_CATALOG, get_metric_value
from .policy_compare import ACTION_CONTROL_FIELDS, compare_policy_metrics


def _load_verified(path: Path) -> tuple[dict[str, Any], bool]:
    report = json.loads(path.read_text(encoding="utf-8"))
    supplied = report.get("metrics_fingerprint")
    payload = {key: value for key, value in report.items() if key != "metrics_fingerprint"}
    return report, isinstance(supplied, str) and canonical_sha256(payload) == supplied


def _population(report: dict[str, Any]) -> dict[str, int]:
    return {job_type: report["jobs"][job_type]["job_count"] for job_type in ("HP", "Spot")}


def _complete(report: dict[str, Any]) -> bool:
    return all(
        report["jobs"][job_type]["completed_count"] == report["jobs"][job_type]["job_count"]
        for job_type in ("HP", "Spot")
    )


def _action(report: dict[str, Any]) -> dict[str, Any]:
    policy = report.get("policy", {})
    return {key: policy.get(key) for key in sorted(ACTION_CONTROL_FIELDS)}


def _execution_controls(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in report.get("policy", {}).items()
        if key not in ACTION_CONTROL_FIELDS
    }


def compare_policy_portfolio(metrics_paths: list[Path]) -> dict[str, Any]:
    if not 3 <= len(metrics_paths) <= 5:
        raise ValueError("A policy portfolio requires between 3 and 5 metrics reports")

    loaded = [_load_verified(path) for path in metrics_paths]
    reports = [report for report, _ in loaded]
    actions = [_action(report) for report in reports]
    criteria = {
        "candidate_count_between_3_and_5": True,
        "metrics_schema_supported": all(
            report.get("schema_version") == "schednav.metrics-report/v1" for report in reports
        ),
        "metrics_fingerprints_valid": all(valid for _, valid in loaded),
        "source_match": len({canonical_sha256(report.get("source")) for report in reports}) == 1
        and bool(reports[0].get("source")),
        "trace_id_match": len({report.get("trace_id") for report in reports}) == 1,
        "window_match": len({canonical_sha256(report.get("window_seconds")) for report in reports}) == 1,
        "population_match": len({canonical_sha256(_population(report)) for report in reports}) == 1,
        "populations_complete": all(_complete(report) for report in reports),
        "event_ledgers_available": all(
            report.get("preemption_events", {}).get("available") is True for report in reports
        ),
        "event_ledgers_consistent": all(
            report.get("preemption_events", {}).get("consistent_with_job_csv") is True
            for report in reports
        ),
        "run_ledgers_available": all(
            report.get("spot_runs", {}).get("available") is True for report in reports
        ),
        "run_ledgers_consistent": all(
            report.get("spot_runs", {}).get("consistent_with_job_csv") is True
            for report in reports
        ),
        "guarantee_ledgers_available": all(
            report.get("spot_guarantee", {}).get("available") is True for report in reports
        ),
        "guarantee_ledgers_consistent": all(
            report.get("spot_guarantee", {}).get("consistent_with_preemption_events") is True
            for report in reports
        ),
        "policy_actions_unique": len({canonical_sha256(action) for action in actions}) == len(actions),
        "execution_controls_match": len(
            {canonical_sha256(_execution_controls(report)) for report in reports}
        )
        == 1,
    }

    candidates = []
    for report, action in zip(reports, actions):
        candidates.append(
            {
                "action": action,
                "policy_fingerprint": report.get("policy_fingerprint"),
                "metrics_fingerprint": report.get("metrics_fingerprint"),
                "metrics": {
                    name: get_metric_value(report, name) for name in METRIC_CATALOG
                },
            }
        )

    pairwise = []
    for left_index, right_index in combinations(range(len(metrics_paths)), 2):
        comparison = compare_policy_metrics(metrics_paths[left_index], metrics_paths[right_index])
        pairwise.append(
            {
                "left_policy_fingerprint": reports[left_index].get("policy_fingerprint"),
                "right_policy_fingerprint": reports[right_index].get("policy_fingerprint"),
                "comparable": comparison["comparable"],
                "metric_deltas": comparison["metric_deltas"],
                "comparison_fingerprint": comparison["comparison_fingerprint"],
            }
        )

    result: dict[str, Any] = {
        "schema_version": "schednav.policy-portfolio/v1",
        "comparable": all(criteria.values()) and all(item["comparable"] for item in pairwise),
        "criteria": criteria,
        "source": reports[0].get("source"),
        "trace_id": reports[0].get("trace_id"),
        "window_seconds": reports[0].get("window_seconds"),
        "population": _population(reports[0]) if criteria["population_match"] else None,
        "candidates": candidates,
        "pairwise": pairwise,
        "definition": "This report preserves three to five simulation-backed candidates and right-minus-left pairwise deltas. It does not apply SLOs, rank candidates, or select a winner.",
    }
    result["portfolio_fingerprint"] = canonical_sha256(result)
    return result
