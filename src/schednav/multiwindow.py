"""Deterministic window selection and aggregate evidence for multi-window studies."""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import median
from typing import Any

from .contracts import canonical_sha256
from .native_trace import CanonicalTrace


@dataclass(frozen=True)
class WindowCandidate:
    start_seconds: float
    end_seconds: float
    hp_job_count: int
    spot_job_count: int
    hp_requested_gpus: float
    spot_requested_gpus: float
    spot_requested_gpu_share: float
    combined_peak_active_pressure: float
    combined_mean_active_pressure: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_seconds": {
                "start": self.start_seconds,
                "end": self.end_seconds,
            },
            "population": {
                "HP": {
                    "job_count": self.hp_job_count,
                    "requested_gpus": self.hp_requested_gpus,
                },
                "Spot": {
                    "job_count": self.spot_job_count,
                    "requested_gpus": self.spot_requested_gpus,
                },
            },
            "spot_requested_gpu_share": self.spot_requested_gpu_share,
            "combined_peak_active_pressure": self.combined_peak_active_pressure,
            "combined_mean_active_pressure": self.combined_mean_active_pressure,
        }


def scan_complete_daily_windows(
    trace: CanonicalTrace,
    *,
    window_size_seconds: int = 86400,
    sample_interval_seconds: int = 3600,
    min_hp_jobs: int = 20,
    min_spot_jobs: int = 20,
) -> list[WindowCandidate]:
    """Scan complete origin-aligned windows without repeatedly traversing every job."""
    if trace.evaluation_start_seconds is not None:
        raise ValueError("Window selection requires a full prefix trace, not an evaluation trace")
    if window_size_seconds <= 0 or sample_interval_seconds <= 0:
        raise ValueError("Window and sample intervals must be positive")
    if window_size_seconds % sample_interval_seconds != 0:
        raise ValueError("window_size_seconds must be divisible by sample_interval_seconds")
    if min_hp_jobs <= 0 or min_spot_jobs <= 0:
        raise ValueError("Minimum HP and Spot populations must be positive")

    max_submit = max(job.submit_time_seconds for job in trace.jobs)
    complete_window_count = int((max_submit + 1) // window_size_seconds)
    if complete_window_count <= 0:
        return []
    samples_per_window = window_size_seconds // sample_interval_seconds
    sample_count = complete_window_count * samples_per_window
    active_deltas = [0.0 for _ in range(sample_count + 1)]
    arrivals = [
        {
            "HP": {"job_count": 0, "requested_gpus": 0.0},
            "Spot": {"job_count": 0, "requested_gpus": 0.0},
        }
        for _ in range(complete_window_count)
    ]

    for job in trace.jobs:
        window_index = int(job.submit_time_seconds // window_size_seconds)
        if window_index < complete_window_count:
            entry = arrivals[window_index][job.service_class]
            entry["job_count"] += 1
            entry["requested_gpus"] += job.gpu_count

        first_sample = math.ceil(job.submit_time_seconds / sample_interval_seconds)
        last_sample = math.ceil(
            (job.submit_time_seconds + job.duration_seconds) / sample_interval_seconds
        )
        first_sample = max(0, min(first_sample, sample_count))
        last_sample = max(0, min(last_sample, sample_count))
        if first_sample < last_sample:
            active_deltas[first_sample] += job.gpu_count
            active_deltas[last_sample] -= job.gpu_count

    active_samples: list[float] = []
    active = 0.0
    for delta in active_deltas[:-1]:
        active += delta
        active_samples.append(active)

    candidates: list[WindowCandidate] = []
    for index, population in enumerate(arrivals):
        hp = population["HP"]
        spot = population["Spot"]
        if hp["job_count"] < min_hp_jobs or spot["job_count"] < min_spot_jobs:
            continue
        samples = active_samples[
            index * samples_per_window : (index + 1) * samples_per_window
        ]
        requested = hp["requested_gpus"] + spot["requested_gpus"]
        start = float(index * window_size_seconds)
        candidates.append(
            WindowCandidate(
                start_seconds=start,
                end_seconds=start + window_size_seconds - 1,
                hp_job_count=int(hp["job_count"]),
                spot_job_count=int(spot["job_count"]),
                hp_requested_gpus=float(hp["requested_gpus"]),
                spot_requested_gpus=float(spot["requested_gpus"]),
                spot_requested_gpu_share=round(
                    float(spot["requested_gpus"]) / requested, 6
                ),
                combined_peak_active_pressure=round(
                    max(samples) / trace.capacity_gpus, 6
                ),
                combined_mean_active_pressure=round(
                    sum(samples) / len(samples) / trace.capacity_gpus, 6
                ),
            )
        )
    return candidates


def _balanced_partitions(values: list[Any], count: int) -> list[list[Any]]:
    if count <= 0 or len(values) < count:
        raise ValueError("Every requested stratum must contain at least one window")
    quotient, remainder = divmod(len(values), count)
    partitions: list[list[Any]] = []
    offset = 0
    for index in range(count):
        size = quotient + (1 if index < remainder else 0)
        partitions.append(values[offset : offset + size])
        offset += size
    return partitions


def select_stratified_windows(
    candidates: list[WindowCandidate],
    *,
    pressure_strata: int = 3,
    spot_share_strata: int = 4,
) -> list[dict[str, Any]]:
    """Select one deterministic medoid from each pressure-by-Spot-share cell."""
    pressure_groups = _balanced_partitions(
        sorted(
            candidates,
            key=lambda item: (item.combined_peak_active_pressure, item.start_seconds),
        ),
        pressure_strata,
    )
    selected: list[dict[str, Any]] = []
    for pressure_index, pressure_group in enumerate(pressure_groups, start=1):
        spot_groups = _balanced_partitions(
            sorted(
                pressure_group,
                key=lambda item: (item.spot_requested_gpu_share, item.start_seconds),
            ),
            spot_share_strata,
        )
        for spot_index, cell in enumerate(spot_groups, start=1):
            center_pressure = median(
                item.combined_peak_active_pressure for item in cell
            )
            center_spot_share = median(item.spot_requested_gpu_share for item in cell)
            pressure_span = max(
                item.combined_peak_active_pressure for item in cell
            ) - min(item.combined_peak_active_pressure for item in cell)
            spot_span = max(item.spot_requested_gpu_share for item in cell) - min(
                item.spot_requested_gpu_share for item in cell
            )

            def distance(item: WindowCandidate) -> tuple[float, float]:
                normalized_pressure = (
                    abs(item.combined_peak_active_pressure - center_pressure)
                    / pressure_span
                    if pressure_span
                    else 0.0
                )
                normalized_spot = (
                    abs(item.spot_requested_gpu_share - center_spot_share) / spot_span
                    if spot_span
                    else 0.0
                )
                return normalized_pressure + normalized_spot, item.start_seconds

            chosen = min(cell, key=distance)
            value = chosen.as_dict()
            value["stratum"] = {
                "pressure": pressure_index,
                "spot_share": spot_index,
                "cell_size": len(cell),
            }
            selected.append(value)
    return selected


def build_window_selection_report(
    trace: CanonicalTrace,
    *,
    window_size_seconds: int = 86400,
    sample_interval_seconds: int = 3600,
    min_hp_jobs: int = 20,
    min_spot_jobs: int = 20,
    pressure_strata: int = 3,
    spot_share_strata: int = 4,
) -> dict[str, Any]:
    candidates = scan_complete_daily_windows(
        trace,
        window_size_seconds=window_size_seconds,
        sample_interval_seconds=sample_interval_seconds,
        min_hp_jobs=min_hp_jobs,
        min_spot_jobs=min_spot_jobs,
    )
    selected = select_stratified_windows(
        candidates,
        pressure_strata=pressure_strata,
        spot_share_strata=spot_share_strata,
    )
    candidate_payload = [candidate.as_dict() for candidate in candidates]
    report: dict[str, Any] = {
        "schema_version": "schednav.multiwindow-selection/v1",
        "trace_id": trace.trace_id,
        "trace_fingerprint": trace.fingerprint,
        "source": trace.source,
        "capacity_gpus": trace.capacity_gpus,
        "eligibility": {
            "origin_aligned_complete_windows_only": True,
            "window_size_seconds": window_size_seconds,
            "sample_interval_seconds": sample_interval_seconds,
            "min_hp_jobs": min_hp_jobs,
            "min_spot_jobs": min_spot_jobs,
        },
        "selection_method": {
            "name": "pressure-by-spot-share-stratified-medoid",
            "pressure_strata": pressure_strata,
            "spot_share_strata_per_pressure": spot_share_strata,
            "tie_breaker": "earliest_window_start",
            "selected_before_simulation": True,
        },
        "eligible_window_count": len(candidates),
        "eligible_windows_fingerprint": canonical_sha256(candidate_payload),
        "selected_window_count": len(selected),
        "selected_windows": selected,
        "definition": "Windows are selected before policy simulation by balanced peak-pressure ranks, then balanced Spot-request-share ranks within each pressure stratum. One normalized medoid is selected per cell.",
    }
    report["selection_fingerprint"] = canonical_sha256(report)
    return report


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        raise ValueError("Aggregate statistics require at least one value")
    return {
        "mean": round(sum(values) / len(values), 9),
        "median": round(float(median(values)), 9),
        "min": round(min(values), 9),
        "max": round(max(values), 9),
    }


def aggregate_multiwindow_records(
    records: list[dict[str, Any]],
    *,
    selection_fingerprint: str,
    baseline_action_id: str = "native-fifo",
) -> dict[str, Any]:
    """Aggregate completed window records without inventing a cross-window winner."""
    if not records:
        raise ValueError("At least one window record is required")
    window_ids = [str(record["window_id"]) for record in records]
    if len(set(window_ids)) != len(window_ids):
        raise ValueError("Multi-window records require unique window IDs")
    action_sets = [
        {str(policy["action_id"]) for policy in record["policies"]}
        for record in records
    ]
    if not action_sets or any(actions != action_sets[0] for actions in action_sets):
        raise ValueError("Every window must evaluate the same policy action set")
    if baseline_action_id not in action_sets[0]:
        raise ValueError("The declared baseline action is missing")

    by_action: dict[str, list[dict[str, Any]]] = {
        action_id: [] for action_id in sorted(action_sets[0])
    }
    selection_status_counts: dict[str, int] = {}
    frontier_frequency = {action_id: 0 for action_id in sorted(action_sets[0])}
    best_uplifts: list[float] = []
    all_hard_pass_window_count = 0
    for record in records:
        policies = {str(item["action_id"]): item for item in record["policies"]}
        for action_id, policy in policies.items():
            by_action[action_id].append(policy)
        status = str(record["ranking"]["selection_status"])
        selection_status_counts[status] = selection_status_counts.get(status, 0) + 1
        for action_id in record["ranking"]["selected_action_ids"]:
            frontier_frequency[str(action_id)] += 1
        hard_pass = [
            policy for policy in policies.values() if policy["hard_slo_passed"] is True
        ]
        if len(hard_pass) == len(policies):
            all_hard_pass_window_count += 1
        baseline_allocation = float(policies[baseline_action_id]["allocation_rate_mean"])
        if hard_pass:
            best_uplifts.append(
                max(float(policy["allocation_rate_mean"]) for policy in hard_pass)
                - baseline_allocation
            )

    policy_summaries: dict[str, Any] = {}
    for action_id, policies in by_action.items():
        allocations = [float(item["allocation_rate_mean"]) for item in policies]
        deltas = []
        jct_regressions = []
        for record, policy in zip(records, policies):
            baseline = next(
                item
                for item in record["policies"]
                if item["action_id"] == baseline_action_id
            )
            baseline_allocation = float(baseline["allocation_rate_mean"])
            deltas.append(float(policy["allocation_rate_mean"]) - baseline_allocation)
            baseline_jct = float(baseline["hp_jct_p95_seconds"])
            jct_regressions.append(
                (float(policy["hp_jct_p95_seconds"]) - baseline_jct) / baseline_jct
            )
        policy_summaries[action_id] = {
            "window_count": len(policies),
            "deterministic_window_count": sum(
                item["deterministic_repetitions"] is True for item in policies
            ),
            "hard_slo_pass_count": sum(
                item["hard_slo_passed"] is True for item in policies
            ),
            "hard_slo_pass_rate": round(
                sum(item["hard_slo_passed"] is True for item in policies)
                / len(policies),
                9,
            ),
            "allocation_soft_target_pass_count": sum(
                item["allocation_soft_target_met"] is True for item in policies
            ),
            "allocation_rate_mean": _stats(allocations),
            "allocation_delta_vs_fifo": {
                **_stats(deltas),
                "positive_window_count": sum(value > 0 for value in deltas),
                "equal_window_count": sum(value == 0 for value in deltas),
                "negative_window_count": sum(value < 0 for value in deltas),
            },
            "hp_p95_jct_regression_vs_fifo": _stats(jct_regressions),
            "spot_eviction_rate_per_run": _stats(
                [float(item["spot_eviction_rate_per_run"]) for item in policies]
            ),
        }

    report: dict[str, Any] = {
        "schema_version": "schednav.multiwindow-summary/v1",
        "selection_fingerprint": selection_fingerprint,
        "window_count": len(records),
        "baseline_action_id": baseline_action_id,
        "all_hard_slo_pass_window_count": all_hard_pass_window_count,
        "selection_status_counts": selection_status_counts,
        "frontier_action_window_frequency": frontier_frequency,
        "best_hard_pass_allocation_uplift_vs_fifo": {
            **_stats(best_uplifts),
            "positive_window_count": sum(value > 0 for value in best_uplifts),
            "equal_window_count": sum(value == 0 for value in best_uplifts),
            "negative_window_count": sum(value < 0 for value in best_uplifts),
        },
        "policies": policy_summaries,
        "windows": records,
        "definition": "This aggregate reports per-policy robustness and the SLO-declared eligible frontier across preselected windows. It does not create a cross-window weighted score or declare a universal winner.",
    }
    report["multiwindow_fingerprint"] = canonical_sha256(report)
    return report
