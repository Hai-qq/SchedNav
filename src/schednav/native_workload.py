"""Backend-neutral workload analysis over the canonical SchedNav trace."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .contracts import canonical_sha256
from .native_trace import CanonicalTrace, load_canonical_trace


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
        "p50": round(float(_quantile(values, 0.50)), 6),
        "p95": round(float(_quantile(values, 0.95)), 6),
        "max": round(max(values), 6),
        "cv": round(math.sqrt(variance) / mean, 6) if mean else None,
    }


def analyze_canonical_workload(
    trace: CanonicalTrace,
    *,
    evaluation_start_seconds: float | None = None,
    evaluation_end_seconds: float | None = None,
    sample_interval_seconds: int = 3600,
) -> dict[str, Any]:
    if sample_interval_seconds <= 0:
        raise ValueError("sample_interval_seconds must be positive")
    start = (
        (
            trace.evaluation_start_seconds
            if trace.evaluation_start_seconds is not None
            else min(job.submit_time_seconds for job in trace.jobs)
        )
        if evaluation_start_seconds is None
        else float(evaluation_start_seconds)
    )
    end = (
        (
            trace.evaluation_end_seconds
            if trace.evaluation_end_seconds is not None
            else max(job.submit_time_seconds for job in trace.jobs)
        )
        if evaluation_end_seconds is None
        else float(evaluation_end_seconds)
    )
    if start < 0 or end < start:
        raise ValueError("Expected 0 <= evaluation_start_seconds <= evaluation_end_seconds")
    samples: list[float] = []
    current = start
    while current <= end:
        samples.append(current)
        current += sample_interval_seconds
    if not samples:
        samples = [start]

    population: dict[str, dict[str, Any]] = {
        service_class: {
            "job_count": 0,
            "requested_gpus": 0,
            "requested_gpu_hours": 0.0,
            "durations": [],
        }
        for service_class in ("HP", "Spot")
    }
    arrivals = {service_class: [0.0 for _ in samples] for service_class in population}
    active = {service_class: [0.0 for _ in samples] for service_class in population}
    for job in trace.jobs:
        if start <= job.submit_time_seconds <= end:
            entry = population[job.service_class]
            entry["job_count"] += 1
            entry["requested_gpus"] += job.gpu_count
            entry["requested_gpu_hours"] += job.gpu_count * job.duration_seconds / 3600
            entry["durations"].append(job.duration_seconds)
            bucket = min(
                int((job.submit_time_seconds - start) // sample_interval_seconds),
                len(samples) - 1,
            )
            arrivals[job.service_class][bucket] += job.gpu_count
        for index, sample in enumerate(samples):
            if job.submit_time_seconds <= sample < job.submit_time_seconds + job.duration_seconds:
                active[job.service_class][index] += job.gpu_count

    for service_class, entry in population.items():
        durations = entry.pop("durations")
        entry["requested_gpu_hours"] = round(entry["requested_gpu_hours"], 6)
        entry["duration_seconds"] = _stats(durations)
        entry["arrival_requested_gpus"] = _stats(arrivals[service_class])
        entry["sampled_active_requested_gpus"] = _stats(active[service_class])
    combined = [hp + spot for hp, spot in zip(active["HP"], active["Spot"])]
    total_requested = sum(entry["requested_gpus"] for entry in population.values())
    capacity = trace.capacity_gpus
    report: dict[str, Any] = {
        "schema_version": "schednav.workload-summary/v2",
        "trace_id": trace.trace_id,
        "trace_fingerprint": trace.fingerprint,
        "time_origin": trace.time_origin,
        "source": trace.source,
        "capacity_gpus": capacity,
        "window_seconds": {"start": start, "end": end},
        "sample_interval_seconds": sample_interval_seconds,
        "population": population,
        "regime_signals": {
            "spot_requested_gpu_share": (
                round(population["Spot"]["requested_gpus"] / total_requested, 6)
                if total_requested
                else None
            ),
            "hp_peak_active_pressure": round(max(active["HP"]) / capacity, 6),
            "spot_peak_active_pressure": round(max(active["Spot"]) / capacity, 6),
            "combined_peak_active_pressure": round(max(combined) / capacity, 6),
            "dominant_arrival_type": (
                "balanced"
                if population["HP"]["requested_gpus"] == population["Spot"]["requested_gpus"]
                else max(("HP", "Spot"), key=lambda key: population[key]["requested_gpus"])
            ),
        },
        "definitions": {
            "requested_gpus": "Sum of requested GPUs for arrivals in the evaluation window.",
            "sampled_active_requested_gpus": "Trace-intended concurrent demand at sample timestamps; this is not simulated allocation.",
            "pressure": "Sampled active requested GPUs divided by physical GPU capacity and may exceed one.",
        },
    }
    report["workload_fingerprint"] = canonical_sha256(report)
    return report


def analyze_trace_file(
    trace_path: Path,
    *,
    evaluation_start_seconds: float | None = None,
    evaluation_end_seconds: float | None = None,
    sample_interval_seconds: int = 3600,
) -> dict[str, Any]:
    return analyze_canonical_workload(
        load_canonical_trace(trace_path),
        evaluation_start_seconds=evaluation_start_seconds,
        evaluation_end_seconds=evaluation_end_seconds,
        sample_interval_seconds=sample_interval_seconds,
    )
