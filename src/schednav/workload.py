"""Structured workload analysis for one real trace window and GPU model."""

from __future__ import annotations

import csv
from datetime import datetime
import math
from pathlib import Path
from typing import Any

from .contracts import ALIBABA_TRACE_ORIGIN, canonical_sha256
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


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "p50": None, "p95": None, "max": None, "cv": None}
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return {
        "mean": round(mean, 6),
        "p50": round(_quantile(values, 0.5), 6),
        "p95": round(_quantile(values, 0.95), 6),
        "max": round(max(values), 6),
        "cv": round(math.sqrt(variance) / mean, 6) if mean else None,
    }


def analyze_workload(
    trace_dir: Path,
    gpu_model: str,
    evaluation_start: str,
    evaluation_end: str,
    sample_interval_seconds: int = 3600,
) -> dict[str, Any]:
    origin = datetime.fromisoformat(ALIBABA_TRACE_ORIGIN)
    start = int((datetime.fromisoformat(evaluation_start) - origin).total_seconds())
    end = int((datetime.fromisoformat(evaluation_end) - origin).total_seconds())
    if not 0 <= start <= end or sample_interval_seconds <= 0:
        raise ValueError("Expected origin <= evaluation_start <= evaluation_end and a positive sample interval")
    node_path = trace_dir / "node_info_df.csv"
    job_path = trace_dir / "job_info_df.csv"
    capacity = 0.0
    with node_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["gpu_model"] == gpu_model:
                capacity += float(row["gpu_capacity_num"])
    if capacity <= 0:
        raise ValueError(f"GPU model has no trace capacity: {gpu_model}")

    samples = list(range(start, end + 1, sample_interval_seconds))
    arrival_buckets = {
        "HP": [0.0 for _ in samples],
        "Spot": [0.0 for _ in samples],
    }
    active_buckets = {
        "HP": [0.0 for _ in samples],
        "Spot": [0.0 for _ in samples],
    }
    population: dict[str, dict[str, Any]] = {
        job_type: {"job_count": 0, "requested_gpus": 0.0, "requested_gpu_hours": 0.0, "durations": []}
        for job_type in ("HP", "Spot")
    }
    relevant_jobs: list[tuple[str, float, float, float]] = []
    with job_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["gpu_model"] != gpu_model or row["job_type"] not in population:
                continue
            job_type = row["job_type"]
            submit = float(row["submit_time"])
            duration = max(float(row["duration"]), 0.0)
            demand = float(row["gpu_request"]) * int(row["worker_num"])
            included_for_active = job_type == "HP" and submit <= end or job_type == "Spot" and start <= submit <= end
            if included_for_active:
                relevant_jobs.append((job_type, submit, submit + duration, demand))
            if start <= submit <= end:
                bucket_index = min(int((submit - start) // sample_interval_seconds), len(samples) - 1)
                arrival_buckets[job_type][bucket_index] += demand
                entry = population[job_type]
                entry["job_count"] += 1
                entry["requested_gpus"] += demand
                entry["requested_gpu_hours"] += demand * duration / 3600
                entry["durations"].append(duration)

    for index, sample_time in enumerate(samples):
        for job_type, submit, finish, demand in relevant_jobs:
            if submit <= sample_time < finish:
                active_buckets[job_type][index] += demand

    for job_type, entry in population.items():
        durations = entry.pop("durations")
        entry["requested_gpus"] = round(entry["requested_gpus"], 6)
        entry["requested_gpu_hours"] = round(entry["requested_gpu_hours"], 6)
        entry["duration_seconds"] = _stats(durations)
        entry["hourly_arrival_requested_gpus"] = _stats(arrival_buckets[job_type])
        entry["sampled_active_requested_gpus"] = _stats(active_buckets[job_type])

    combined_active = [hp + spot for hp, spot in zip(active_buckets["HP"], active_buckets["Spot"])]
    total_requested = sum(entry["requested_gpus"] for entry in population.values())
    report: dict[str, Any] = {
        "schema_version": "schednav.workload-summary/v1",
        "trace_origin": ALIBABA_TRACE_ORIGIN,
        "gpu_model": gpu_model,
        "capacity_gpus": int(capacity) if capacity.is_integer() else capacity,
        "window": {"start": evaluation_start, "end": evaluation_end},
        "sample_interval_seconds": sample_interval_seconds,
        "population": population,
        "regime_signals": {
            "spot_requested_gpu_share": round(population["Spot"]["requested_gpus"] / total_requested, 6)
            if total_requested
            else None,
            "hp_peak_active_pressure": round(max(active_buckets["HP"]) / capacity, 6),
            "spot_peak_active_pressure": round(max(active_buckets["Spot"]) / capacity, 6),
            "combined_peak_active_pressure": round(max(combined_active) / capacity, 6),
            "dominant_arrival_type": "balanced"
            if population["HP"]["requested_gpus"] == population["Spot"]["requested_gpus"]
            else max(("HP", "Spot"), key=lambda key: population[key]["requested_gpus"]),
        },
        "source_evidence": {
            "node_info_sha256": sha256_file(node_path),
            "job_info_sha256": sha256_file(job_path),
        },
        "definitions": {
            "requested_gpus": "Sum of gpu_request multiplied by worker_num for arrivals in the evaluation window.",
            "sampled_active_requested_gpus": "Trace-intended concurrent demand at sample timestamps; HP includes carry-in and Spot is limited to evaluation-window arrivals. This is not simulated allocation.",
            "pressure": "Sampled active requested GPUs divided by physical GPU capacity; it may exceed 1 because queued demand is not placement.",
        },
    }
    report["workload_fingerprint"] = canonical_sha256(report)
    return report
