"""Canonical metric names shared by comparison and SLO audit."""

from __future__ import annotations

from typing import Any


METRIC_CATALOG = {
    "hp_completion_rate": (("jobs", "HP", "completion_rate"), "higher_is_better"),
    "hp_jct_mean_seconds": (("jobs", "HP", "jct_seconds", "mean"), "lower_is_better"),
    "hp_jct_p50_seconds": (("jobs", "HP", "jct_seconds", "p50"), "lower_is_better"),
    "hp_jct_p95_seconds": (("jobs", "HP", "jct_seconds", "p95"), "lower_is_better"),
    "hp_queue_mean_seconds": (("jobs", "HP", "queue_seconds", "mean"), "lower_is_better"),
    "hp_queue_p95_seconds": (("jobs", "HP", "queue_seconds", "p95"), "lower_is_better"),
    "hp_preempted_job_count": (("jobs", "HP", "preempted_job_count"), "lower_is_better"),
    "spot_completion_rate": (("jobs", "Spot", "completion_rate"), "higher_is_better"),
    "spot_jct_mean_seconds": (("jobs", "Spot", "jct_seconds", "mean"), "lower_is_better"),
    "spot_jct_p50_seconds": (("jobs", "Spot", "jct_seconds", "p50"), "lower_is_better"),
    "spot_jct_p95_seconds": (("jobs", "Spot", "jct_seconds", "p95"), "lower_is_better"),
    "spot_queue_mean_seconds": (("jobs", "Spot", "queue_seconds", "mean"), "lower_is_better"),
    "spot_queue_p95_seconds": (("jobs", "Spot", "queue_seconds", "p95"), "lower_is_better"),
    "spot_preemption_count": (("jobs", "Spot", "preemption_count"), "lower_is_better"),
    "spot_preempted_job_rate": (("jobs", "Spot", "preempted_job_rate"), "lower_is_better"),
    "spot_eviction_rate_per_run": (("preemption_events", "eviction_rate_per_run"), "lower_is_better"),
    "spot_guarantee_success_rate": (("spot_guarantee", "success_rate"), "higher_is_better"),
    "allocation_rate_mean": (("cluster", "allocation_rate_mean"), "higher_is_better"),
    "preemption_added_gpu_seconds": (
        ("preemption_events", "added_gpu_seconds_total"),
        "lower_is_better",
    ),
}


def get_metric_value(report: dict[str, Any], metric: str) -> float | int | None:
    if metric not in METRIC_CATALOG:
        raise ValueError(f"Unsupported metric: {metric}")
    value: Any = report
    for key in METRIC_CATALOG[metric][0]:
        value = value[key]
    return value
