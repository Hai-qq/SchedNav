"""Evidence gate for a GFS run that exercises real Spot preemption."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .contracts import canonical_sha256


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def evaluate_eviction_gate(metrics_path: Path) -> dict[str, Any]:
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    supplied_fingerprint = metrics.get("metrics_fingerprint")
    fingerprint_payload = {key: value for key, value in metrics.items() if key != "metrics_fingerprint"}
    spot = metrics.get("jobs", {}).get("Spot", {})
    evidence = metrics.get("evidence", {})
    events = metrics.get("preemption_events", {})
    job_count = spot.get("job_count")
    completed_count = spot.get("completed_count")
    preemption_count = spot.get("preemption_count")
    preempted_job_count = spot.get("preempted_job_count")
    criteria = {
        "metrics_schema_supported": metrics.get("schema_version") == "schednav.metrics-report/v1",
        "metrics_fingerprint_valid": bool(supplied_fingerprint)
        and canonical_sha256(fingerprint_payload) == supplied_fingerprint,
        "csv_evidence_attested": all(
            isinstance(evidence.get(field), str) and SHA256_PATTERN.fullmatch(evidence[field])
            for field in ("job_csv_sha256", "sequence_csv_sha256", "preemption_event_csv_sha256")
        ),
        "spot_population_nonempty": isinstance(job_count, int) and job_count > 0,
        "spot_population_complete": isinstance(job_count, int)
        and isinstance(completed_count, int)
        and completed_count == job_count,
        "spot_preemption_observed": isinstance(preemption_count, int)
        and isinstance(preempted_job_count, int)
        and preemption_count > 0
        and preempted_job_count > 0,
        "event_ledger_consistent": events.get("available") is True
        and events.get("consistent_with_job_csv") is True
        and events.get("counted_spot_failure_count") == preemption_count
        and events.get("preempted_job_count") == preempted_job_count,
    }
    report: dict[str, Any] = {
        "schema_version": "schednav.eviction-gate-report/v1",
        "metrics_fingerprint": supplied_fingerprint,
        "criteria": criteria,
        "gate_passed": all(criteria.values()),
        "observed": {
            "spot_job_count": job_count,
            "spot_completed_count": completed_count,
            "spot_preemption_count": preemption_count,
            "spot_preempted_job_count": preempted_job_count,
            "event_added_gpu_seconds_total": events.get("added_gpu_seconds_total"),
        },
        "definition": "A passing run has attested CSV-derived metrics, completes its evaluation-window Spot population, and records at least one Spot preemption consistently in the job CSV and structured event ledger.",
    }
    report["gate_fingerprint"] = canonical_sha256(report)
    return report
