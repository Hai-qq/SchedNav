"""Deterministic metric extraction from GFS CSV evidence."""

from __future__ import annotations

import csv
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Any

from .contracts import RunSpec, canonical_sha256
from .gfs_adapter import sha256_file


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _summary(values: list[float]) -> dict[str, float | None]:
    return {
        "mean": round(sum(values) / len(values), 6) if values else None,
        "p50": round(value, 6) if (value := _quantile(values, 0.50)) is not None else None,
        "p95": round(value, 6) if (value := _quantile(values, 0.95)) is not None else None,
    }


def _find_evidence(base: Path, suffix: str) -> Path:
    matches = sorted(base.glob(f"cluster/*{suffix}"))
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {suffix} under {base}, got {matches}")
    return matches[0]


def _find_optional_evidence(base: Path, suffix: str) -> Path | None:
    matches = sorted(base.glob(f"cluster/*{suffix}"))
    if len(matches) > 1:
        raise ValueError(f"Expected at most one {suffix} under {base}, got {matches}")
    return matches[0] if matches else None


def _csv_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"Expected CSV boolean, got {value!r}")
    return normalized == "true"


def extract_metrics(spec: RunSpec, run_manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "succeeded":
        raise ValueError("Metrics require a succeeded run manifest")
    if manifest.get("run_spec_fingerprint") != spec.fingerprint:
        raise ValueError("Run manifest and reproduction config fingerprints differ")
    run_dir = run_manifest_path.parent
    evidence_base = run_dir / "gfs-output" / str(manifest["run_id"])
    job_path = _find_evidence(evidence_base, "_log.csv")
    sequence_path = _find_evidence(evidence_base, "_seq.csv")
    event_path = _find_optional_evidence(evidence_base, "_preemption_events.csv")
    run_event_path = _find_optional_evidence(evidence_base, "_spot_run_events.csv")
    guarantee_event_path = _find_optional_evidence(evidence_base, "_spot_guarantee_events.csv")
    evaluation_start = spec.window.seconds_from_origin(spec.window.evaluation_start)
    evaluation_end = spec.window.seconds_from_origin(spec.window.evaluation_end)

    jobs: list[dict[str, str]] = []
    with job_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            submit_time = float(row["submit_time"])
            if evaluation_start <= submit_time <= evaluation_end:
                jobs.append(row)

    by_type: dict[str, Any] = {}
    for job_type in ("HP", "Spot"):
        selected = [row for row in jobs if row["type"] == job_type]
        completed = [row for row in selected if row["status"] == "end"]
        preemptions = [int(float(row["preempt_times"])) for row in selected]
        by_type[job_type] = {
            "job_count": len(selected),
            "completed_count": len(completed),
            "completion_rate": len(completed) / len(selected) if selected else None,
            "jct_seconds": _summary([float(row["jct"]) for row in completed]),
            "queue_seconds": _summary([float(row["queue"]) for row in completed]),
            "preemption_count": sum(preemptions),
            "preempted_job_count": sum(value > 0 for value in preemptions),
            "preempted_job_rate": sum(value > 0 for value in preemptions) / len(selected) if selected else None,
        }

    allocation_samples: list[float] = []
    sample_times: list[int] = []
    with sequence_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            sample_time = int(float(row["time"]))
            if evaluation_start <= sample_time <= evaluation_end:
                sample_times.append(sample_time)
                allocation_samples.append(float(row["gpu_utilization"]))
    intervals = [right - left for left, right in zip(sample_times, sample_times[1:])]

    selected_events: list[dict[str, str]] = []
    if event_path is not None:
        with event_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                submit_time = float(row["preempted_submit_time"])
                if row["preempted_type"] == "Spot" and evaluation_start <= submit_time <= evaluation_end:
                    selected_events.append(row)
    counted_events = [row for row in selected_events if _csv_bool(row["counted_as_spot_failure"])]
    event_job_indexes = {row["preempted_job_index"] for row in counted_events}
    job_preemption_count = by_type["Spot"]["preemption_count"]
    job_preempted_count = by_type["Spot"]["preempted_job_count"]
    event_consistent = event_path is not None and (
        len(counted_events) == job_preemption_count and len(event_job_indexes) == job_preempted_count
    )
    spot_jobs = [row for row in jobs if row["type"] == "Spot"]
    selected_run_events: list[dict[str, str]] = []
    if run_event_path is not None:
        with run_event_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                submit_time = float(row["job_submit_time"])
                if row["job_type"] == "Spot" and evaluation_start <= submit_time <= evaluation_end:
                    selected_run_events.append(row)
    run_ordinals_by_job: dict[str, list[int]] = {}
    for row in selected_run_events:
        run_ordinals_by_job.setdefault(row["job_index"], []).append(int(float(row["run_ordinal_for_job"])))
    run_ledger_consistent = run_event_path is not None
    for job in spot_jobs:
        started = float(job["start_time"]) < float(sys.maxsize)
        expected_run_count = int(float(job["preempt_times"])) + 1 if started else 0
        actual_ordinals = sorted(run_ordinals_by_job.pop(job["job_index"], []))
        if actual_ordinals != list(range(1, expected_run_count + 1)):
            run_ledger_consistent = False
    if run_ordinals_by_job:
        run_ledger_consistent = False
    spot_run_count = len(selected_run_events)
    spot_eviction_rate_per_run = (
        len(selected_events) / spot_run_count if spot_run_count > 0 else None
    )

    selected_guarantee_events: list[dict[str, str]] = []
    if guarantee_event_path is not None:
        with guarantee_event_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                submit_time = float(row["job_submit_time"])
                if row["job_type"] == "Spot" and evaluation_start <= submit_time <= evaluation_end:
                    selected_guarantee_events.append(row)
    invalid_outcomes = {
        row["outcome"] for row in selected_guarantee_events if row["outcome"] not in {"succeeded", "failed"}
    }
    if invalid_outcomes:
        raise ValueError(f"Unsupported Spot guarantee outcomes: {sorted(invalid_outcomes)}")
    guarantee_succeeded = sum(row["outcome"] == "succeeded" for row in selected_guarantee_events)
    guarantee_failed = sum(row["outcome"] == "failed" for row in selected_guarantee_events)
    guarantee_total = guarantee_succeeded + guarantee_failed
    guarantee_consistent = guarantee_event_path is not None and guarantee_failed == len(counted_events)
    report = {
        "schema_version": "schednav.metrics-report/v1",
        "run_spec_fingerprint": spec.fingerprint,
        "policy_fingerprint": spec.policy.fingerprint,
        "policy": asdict(spec.policy),
        "source": {
            "gfs_commit": manifest["gfs_commit"],
            "gfs_patch_sha256": manifest["gfs_patch_sha256"],
            "trace_commit": manifest["trace_commit"],
        },
        "trace_id": manifest["trace_id"],
        "window_seconds": {"start": evaluation_start, "end": evaluation_end},
        "jobs": by_type,
        "cluster": {
            "allocation_rate_mean": round(sum(allocation_samples) / len(allocation_samples), 6) if allocation_samples else None,
            "sample_count": len(allocation_samples),
            "sample_interval_seconds": min(intervals) if intervals and len(set(intervals)) == 1 else None,
        },
        "preemption_events": {
            "available": event_path is not None,
            "event_count": len(selected_events),
            "counted_spot_failure_count": len(counted_events),
            "preempted_job_count": len(event_job_indexes),
            "events_during_evaluation_window": sum(
                _csv_bool(row["event_in_evaluation_window"]) for row in counted_events
            ),
            "events_during_drain": sum(
                not _csv_bool(row["event_in_evaluation_window"]) for row in counted_events
            ),
            "rollback_seconds_total": round(sum(float(row["rollback_seconds"]) for row in counted_events), 6),
            "overhead_seconds_total": round(sum(float(row["overhead_seconds"]) for row in counted_events), 6),
            "added_gpu_seconds_total": round(sum(float(row["added_gpu_seconds"]) for row in counted_events), 6),
            "consistent_with_job_csv": event_consistent,
            "spot_run_count": spot_run_count,
            "eviction_rate_per_run": spot_eviction_rate_per_run,
        },
        "spot_runs": {
            "available": run_event_path is not None,
            "event_count": spot_run_count,
            "events_during_evaluation_window": sum(
                _csv_bool(row["event_in_evaluation_window"]) for row in selected_run_events
            ),
            "events_during_drain": sum(
                not _csv_bool(row["event_in_evaluation_window"]) for row in selected_run_events
            ),
            "consistent_with_job_csv": run_ledger_consistent,
        },
        "spot_guarantee": {
            "available": guarantee_event_path is not None,
            "event_count": guarantee_total,
            "succeeded_count": guarantee_succeeded,
            "failed_count": guarantee_failed,
            "success_rate": guarantee_succeeded / guarantee_total if guarantee_total > 0 else None,
            "consistent_with_preemption_events": guarantee_consistent,
        },
        "evidence": {
            "job_csv_sha256": sha256_file(job_path),
            "sequence_csv_sha256": sha256_file(sequence_path),
            "preemption_event_csv_sha256": sha256_file(event_path) if event_path is not None else None,
            "spot_run_event_csv_sha256": sha256_file(run_event_path) if run_event_path is not None else None,
            "spot_guarantee_event_csv_sha256": (
                sha256_file(guarantee_event_path) if guarantee_event_path is not None else None
            ),
        },
        "definitions": {
            "allocation_rate_mean": "Mean of equally spaced GFS gpu_utilization samples; this is allocation, not GPU core utilization.",
            "preempted_job_rate": "Jobs with preempt_times > 0 divided by jobs of the same type in the evaluation arrival window.",
            "spot_eviction_rate_per_run": "Spot eviction events divided by explicit Spot run-start events for the evaluation arrival population, including run starts and evictions during drain; this follows the GFS paper's evictions-per-runs definition.",
            "spot_guarantee_success_rate": "Succeeded guarantee-period events divided by all succeeded and failed guarantee-period events for Spot jobs in the evaluation arrival population, including events during drain.",
            "checkpoint_events": "The upstream ckpt_times job column remains unusable; checkpoint rollback is instead observed per preemption event.",
            "added_gpu_seconds_total": "Sum of (rollback_seconds + overhead_seconds) multiplied by the preempted job's requested GPUs; this is simulator-added allocation work, not hardware utilization.",
            "population": "Jobs whose submit_time is within the inclusive evaluation window; drain continues until completion.",
        },
    }
    report["metrics_fingerprint"] = canonical_sha256(report)
    return report
