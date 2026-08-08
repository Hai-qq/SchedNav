"""Deterministic trace heuristics for finding eviction simulation candidates."""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .contracts import ALIBABA_TRACE_ORIGIN, canonical_sha256
from .gfs_adapter import sha256_file


SECONDS_PER_DAY = 86_400
NOON_SECONDS = 43_200


def _day_index(value: date, origin: datetime) -> int:
    return (value - origin.date()).days


def scan_eviction_candidates(
    trace_dir: Path,
    earliest_date: str,
    latest_date: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Rank real trace days for simulation; the score is not eviction evidence."""
    node_path = trace_dir / "node_info_df.csv"
    job_path = trace_dir / "job_info_df.csv"
    if not node_path.exists() or not job_path.exists():
        raise FileNotFoundError("trace_dir must contain node_info_df.csv and job_info_df.csv")
    if limit <= 0:
        raise ValueError("limit must be positive")

    origin = datetime.fromisoformat(ALIBABA_TRACE_ORIGIN)
    earliest = date.fromisoformat(earliest_date)
    latest = date.fromisoformat(latest_date)
    if earliest > latest or earliest < origin.date():
        raise ValueError("Expected trace origin <= earliest_date <= latest_date")
    first_day = _day_index(earliest, origin)
    last_day = _day_index(latest, origin)

    capacities: dict[str, float] = {}
    with node_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            model = row["gpu_model"]
            capacities[model] = capacities.get(model, 0.0) + float(row["gpu_capacity_num"])

    buckets: dict[tuple[str, int], dict[str, float | int]] = {}
    with job_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            submit_time = float(row["submit_time"])
            day = int(submit_time // SECONDS_PER_DAY)
            if day < first_day or day > last_day:
                continue
            model = row["gpu_model"]
            if model not in capacities:
                continue
            key = (model, day)
            bucket = buckets.setdefault(
                key,
                {
                    "spot_jobs": 0,
                    "spot_requested_gpus": 0.0,
                    "hp_jobs": 0,
                    "hp_requested_gpus": 0.0,
                    "early_spot_jobs": 0,
                    "early_spot_requested_gpus": 0.0,
                    "early_spot_gpu_hours": 0.0,
                    "late_hp_jobs": 0,
                    "late_hp_requested_gpus": 0.0,
                },
            )
            requested_gpus = float(row["gpu_request"]) * int(row["worker_num"])
            second_of_day = submit_time - day * SECONDS_PER_DAY
            if row["job_type"] == "Spot":
                bucket["spot_jobs"] += 1
                bucket["spot_requested_gpus"] += requested_gpus
                if second_of_day < NOON_SECONDS:
                    remaining_day = SECONDS_PER_DAY - second_of_day
                    overlap_seconds = min(max(float(row["duration"]), 0.0), remaining_day)
                    bucket["early_spot_jobs"] += 1
                    bucket["early_spot_requested_gpus"] += requested_gpus
                    bucket["early_spot_gpu_hours"] += requested_gpus * overlap_seconds / 3600
            elif row["job_type"] == "HP":
                bucket["hp_jobs"] += 1
                bucket["hp_requested_gpus"] += requested_gpus
                if second_of_day >= NOON_SECONDS:
                    bucket["late_hp_jobs"] += 1
                    bucket["late_hp_requested_gpus"] += requested_gpus

    candidates: list[dict[str, Any]] = []
    for (model, day), bucket in buckets.items():
        if not bucket["spot_jobs"]:
            continue
        capacity = capacities[model]
        early_spot_pressure = float(bucket["early_spot_gpu_hours"]) / (capacity * 12)
        late_hp_pressure = float(bucket["late_hp_requested_gpus"]) / capacity
        score = early_spot_pressure * late_hp_pressure
        candidates.append(
            {
                "rank": 0,
                "gpu_model": model,
                "date": date.fromordinal(origin.date().toordinal() + day).isoformat(),
                "capacity_gpus": int(capacity) if capacity.is_integer() else capacity,
                **{
                    name: round(value, 6) if isinstance(value, float) else value
                    for name, value in bucket.items()
                },
                "early_spot_pressure": round(early_spot_pressure, 6),
                "late_hp_pressure": round(late_hp_pressure, 6),
                "candidate_score": round(score, 6),
                "_candidate_score_raw": score,
            }
        )
    candidates.sort(key=lambda item: (-item["_candidate_score_raw"], item["date"], item["gpu_model"]))
    candidates = candidates[:limit]
    for rank, candidate in enumerate(candidates, start=1):
        candidate.pop("_candidate_score_raw")
        candidate["rank"] = rank

    report: dict[str, Any] = {
        "schema_version": "schednav.eviction-window-candidates/v1",
        "trace_origin": ALIBABA_TRACE_ORIGIN,
        "date_range": {"earliest": earliest.isoformat(), "latest": latest.isoformat()},
        "algorithm": "early-spot-late-hp-pressure/v1",
        "source_evidence": {
            "node_info_sha256": sha256_file(node_path),
            "job_info_sha256": sha256_file(job_path),
        },
        "definitions": {
            "early_spot_pressure": "First-half Spot requested GPU-hours overlapping the same day, divided by 12 hours of cluster capacity.",
            "late_hp_pressure": "Second-half HP requested GPUs divided by cluster GPU capacity; arrivals are summed without assuming admission.",
            "candidate_score": "early_spot_pressure multiplied by late_hp_pressure; this ranks simulations and is not evidence that eviction occurred.",
        },
        "candidates": candidates,
    }
    report["scan_fingerprint"] = canonical_sha256(report)
    return report
