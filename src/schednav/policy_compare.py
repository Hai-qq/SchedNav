"""Neutral, evidence-backed comparison of two canonical policy metrics reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import canonical_sha256
from .metric_catalog import METRIC_CATALOG, SUPPORTED_METRICS_SCHEMAS, get_metric_value


ACTION_CONTROL_FIELDS = {
    "action_id",
    "hp_preemption_delay_seconds",
    "scheduler",
    "spot_eviction_budget_rate",
    "preemption_victim_strategy",
    "spot_guarantee_seconds",
    "checkpoint_interval_seconds",
}


INTERPRETATION_CAVEATS = [
    "A comparison may change more than one admitted action control. Metric deltas describe the complete declared policy profiles and do not establish a single-variable causal effect.",
    "Comparability attests the Trace window, population, run controls, evidence, and completion contract. It does not establish causal superiority or select a policy.",
]


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


def compare_policy_metrics(left_path: Path, right_path: Path) -> dict[str, Any]:
    left, left_fingerprint_valid = _load_verified(left_path)
    right, right_fingerprint_valid = _load_verified(right_path)
    left_policy = left.get("policy", {})
    right_policy = right.get("policy", {})
    left_action = {key: left_policy.get(key) for key in sorted(ACTION_CONTROL_FIELDS)}
    right_action = {key: right_policy.get(key) for key in sorted(ACTION_CONTROL_FIELDS)}
    left_execution_controls = {
        key: value for key, value in left_policy.items() if key not in ACTION_CONTROL_FIELDS
    }
    right_execution_controls = {
        key: value for key, value in right_policy.items() if key not in ACTION_CONTROL_FIELDS
    }
    criteria = {
        "metrics_schema_supported": left.get("schema_version") == right.get("schema_version")
        and left.get("schema_version") in SUPPORTED_METRICS_SCHEMAS,
        "metrics_fingerprints_valid": left_fingerprint_valid and right_fingerprint_valid,
        "source_match": left.get("source") == right.get("source") and bool(left.get("source")),
        "trace_id_match": left.get("trace_id") == right.get("trace_id"),
        "window_match": left.get("window_seconds") == right.get("window_seconds"),
        "population_match": _population(left) == _population(right),
        "populations_complete": _complete(left) and _complete(right),
        "event_ledgers_available": left.get("preemption_events", {}).get("available") is True
        and right.get("preemption_events", {}).get("available") is True,
        "event_ledgers_consistent": left.get("preemption_events", {}).get("consistent_with_job_csv") is True
        and right.get("preemption_events", {}).get("consistent_with_job_csv") is True,
        "run_ledgers_available": left.get("spot_runs", {}).get("available") is True
        and right.get("spot_runs", {}).get("available") is True,
        "run_ledgers_consistent": left.get("spot_runs", {}).get("consistent_with_job_csv") is True
        and right.get("spot_runs", {}).get("consistent_with_job_csv") is True,
        "guarantee_ledgers_available": left.get("spot_guarantee", {}).get("available") is True
        and right.get("spot_guarantee", {}).get("available") is True,
        "guarantee_ledgers_consistent": left.get("spot_guarantee", {}).get(
            "consistent_with_preemption_events"
        ) is True
        and right.get("spot_guarantee", {}).get("consistent_with_preemption_events") is True,
        "policy_actions_distinct": left_action != right_action,
        "execution_controls_match": left_execution_controls == right_execution_controls,
    }

    deltas: dict[str, Any] = {}
    for name, (_, direction) in METRIC_CATALOG.items():
        left_value = get_metric_value(left, name)
        right_value = get_metric_value(right, name)
        absolute_delta = None
        relative_delta = None
        if left_value is not None and right_value is not None:
            absolute_delta = round(float(right_value) - float(left_value), 6)
            if float(left_value) != 0:
                relative_delta = round(absolute_delta / float(left_value), 6)
        deltas[name] = {
            "left": left_value,
            "right": right_value,
            "right_minus_left": absolute_delta,
            "relative_to_left": relative_delta,
            "preferred_direction": direction,
        }

    report: dict[str, Any] = {
        "schema_version": "schednav.policy-comparison/v1",
        "comparable": all(criteria.values()),
        "criteria": criteria,
        "left": {
            "scheduler": left_policy.get("scheduler"),
            "action": left_action,
            "policy_fingerprint": left.get("policy_fingerprint"),
            "metrics_fingerprint": left.get("metrics_fingerprint"),
        },
        "right": {
            "scheduler": right_policy.get("scheduler"),
            "action": right_action,
            "policy_fingerprint": right.get("policy_fingerprint"),
            "metrics_fingerprint": right.get("metrics_fingerprint"),
        },
        "population": _population(left) if criteria["population_match"] else None,
        "metric_deltas": deltas,
        "interpretation_caveats": INTERPRETATION_CAVEATS,
        "definition": "Deltas are right minus left. Preferred direction is metadata only; this report does not select a winner or apply SLOs.",
    }
    report["comparison_fingerprint"] = canonical_sha256(report)
    return report
