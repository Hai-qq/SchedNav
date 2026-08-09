"""Run a pre-simulation-stratified SchedNav multi-window experiment."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT_DEFAULT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from schednav.contracts import canonical_sha256
from schednav.multiwindow import (
    aggregate_multiwindow_records,
    build_window_selection_report,
)
from schednav.native_simulator import run_native_simulation
from schednav.native_trace import import_alibaba_trace, load_canonical_trace
from schednav.native_workload import analyze_trace_file
from schednav.policy_portfolio import compare_policy_portfolio
from schednav.policy_rank import rank_audited_policies
from schednav.slo import audit_slo


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _resolve_inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Project-relative path escapes the project: {relative}") from exc
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def _soft_target(audit: dict[str, Any]) -> bool | None:
    for result in audit["results"]:
        if result["id"] == "allocation-soft-target":
            return bool(result["passed"])
    return None


def _run_simulation_once(
    task: tuple[str, str, str, str, int],
) -> tuple[str, str, int, dict[str, str], dict[str, Any]]:
    window_id, trace_path, policy_path, action_id, repetition = task
    result, metrics = run_native_simulation(Path(trace_path), Path(policy_path))
    return (
        window_id,
        action_id,
        repetition,
        {
            "result_fingerprint": result["result_fingerprint"],
            "metrics_fingerprint": metrics["metrics_fingerprint"],
        },
        metrics,
    )


def _run_policy_repetitions(
    traces: list[tuple[str, Path]],
    policies: list[tuple[str, Path]],
    *,
    repetitions: int,
    workers: int,
) -> dict[str, dict[str, list[tuple[int, dict[str, str], dict[str, Any]]]]]:
    tasks = [
        (window_id, str(trace_path), str(policy_path), action_id, repetition)
        for window_id, trace_path in traces
        for action_id, policy_path in policies
        for repetition in range(repetitions)
    ]
    results = {
        window_id: {action_id: [] for action_id, _ in policies}
        for window_id, _ in traces
    }
    completed_count = 0
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_run_simulation_once, task) for task in tasks]
        for future in as_completed(futures):
            window_id, action_id, repetition, receipt, metrics = future.result()
            results[window_id][action_id].append((repetition, receipt, metrics))
            completed_count += 1
            if completed_count % 8 == 0 or completed_count == len(tasks):
                print(f"[simulate] {completed_count}/{len(tasks)} runs", flush=True)
    for window_results in results.values():
        for action_id in window_results:
            window_results[action_id].sort(key=lambda item: item[0])
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(PROJECT_ROOT_DEFAULT))
    parser.add_argument("--dataset-directory", required=True)
    parser.add_argument("--output-directory", required=True)
    parser.add_argument(
        "--action-space",
        default="configs/action_spaces/native-multiwindow-v1.json",
    )
    parser.add_argument("--gpu-model", default="GPU-series-2")
    parser.add_argument("--time-origin", default="2024-03-01 00:00:00")
    parser.add_argument("--window-size-seconds", type=int, default=86400)
    parser.add_argument("--sample-interval-seconds", type=int, default=3600)
    parser.add_argument("--warmup-seconds", type=int, default=30 * 86400)
    parser.add_argument("--min-hp-jobs", type=int, default=20)
    parser.add_argument("--min-spot-jobs", type=int, default=20)
    parser.add_argument("--pressure-strata", type=int, default=3)
    parser.add_argument("--spot-share-strata", type=int, default=4)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    dataset_root = Path(args.dataset_directory).resolve()
    output_root = Path(args.output_directory).resolve()
    if output_root.exists():
        raise FileExistsError(f"Refusing to overwrite output directory: {output_root}")
    if args.repetitions < 2:
        raise ValueError("Multi-window evidence requires at least two repetitions")
    if args.warmup_seconds < 0:
        raise ValueError("warmup-seconds cannot be negative")
    if args.workers <= 0:
        raise ValueError("workers must be positive")
    node_info = dataset_root / "node_info_df.csv"
    job_info = dataset_root / "job_info_df.csv"
    if not node_info.is_file() or not job_info.is_file():
        raise FileNotFoundError(
            "Dataset directory must contain node_info_df.csv and job_info_df.csv"
        )

    action_space_path = _resolve_inside(project_root, args.action_space)
    action_space = json.loads(action_space_path.read_text(encoding="utf-8"))
    policy_paths = [
        _resolve_inside(project_root, relative) for relative in action_space["profiles"]
    ]
    slo_path = project_root / "configs" / "slos" / "schednav-demo-slo-v1.json"
    output_root.mkdir(parents=True, exist_ok=False)

    selection_trace_path = import_alibaba_trace(
        node_info,
        job_info,
        output_root / "selection-trace",
        trace_id=f"alibaba-{args.gpu_model.lower()}-full-selection",
        time_origin=args.time_origin,
        gpu_models={args.gpu_model},
    )
    selection_trace = load_canonical_trace(selection_trace_path)
    selection = build_window_selection_report(
        selection_trace,
        window_size_seconds=args.window_size_seconds,
        sample_interval_seconds=args.sample_interval_seconds,
        min_hp_jobs=args.min_hp_jobs,
        min_spot_jobs=args.min_spot_jobs,
        pressure_strata=args.pressure_strata,
        spot_share_strata=args.spot_share_strata,
    )
    _write_json(output_root / "window-selection.json", selection)
    origin = datetime.fromisoformat(args.time_origin)
    policies = [
        (
            str(json.loads(path.read_text(encoding="utf-8"))["action_id"]),
            path,
        )
        for path in policy_paths
    ]
    prepared_windows: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []

    for window_number, selected in enumerate(selection["selected_windows"], start=1):
        start = float(selected["window_seconds"]["start"])
        end = float(selected["window_seconds"]["end"])
        warmup_start = max(0.0, start - args.warmup_seconds)
        date = (origin + timedelta(seconds=start)).date().isoformat()
        window_id = f"{args.gpu_model}-{date}"
        window_root = output_root / "windows" / date
        print(
            f"[prepare {window_number}/{selection['selected_window_count']}] {window_id}",
            flush=True,
        )
        trace_path = import_alibaba_trace(
            node_info,
            job_info,
            window_root / "trace",
            trace_id=f"alibaba-{args.gpu_model.lower()}-{date}",
            time_origin=args.time_origin,
            gpu_models={args.gpu_model},
            evaluation_start_seconds=start,
            evaluation_end_seconds=end,
            warmup_start_seconds=warmup_start,
        )
        trace = load_canonical_trace(trace_path)
        workload = analyze_trace_file(
            trace_path,
            sample_interval_seconds=args.sample_interval_seconds,
        )
        _write_json(window_root / "workload-summary.json", workload)
        prepared_windows.append(
            {
                "selected": selected,
                "start": start,
                "end": end,
                "warmup_start": warmup_start,
                "date": date,
                "window_id": window_id,
                "window_root": window_root,
                "trace_path": trace_path,
                "trace": trace,
                "workload": workload,
            }
        )

    simulation_results = _run_policy_repetitions(
        [
            (str(window["window_id"]), Path(window["trace_path"]))
            for window in prepared_windows
        ],
        policies,
        repetitions=args.repetitions,
        workers=args.workers,
    )

    for prepared in prepared_windows:
        selected = prepared["selected"]
        start = float(prepared["start"])
        end = float(prepared["end"])
        warmup_start = float(prepared["warmup_start"])
        date = str(prepared["date"])
        window_id = str(prepared["window_id"])
        window_root = Path(prepared["window_root"])
        trace = prepared["trace"]
        workload = prepared["workload"]
        metrics_paths: list[Path] = []
        policy_records: list[dict[str, Any]] = []
        determinism: dict[str, Any] = {}
        for action_id, policy_path in policies:
            completed = simulation_results[window_id][action_id]
            repetitions = [item[1] for item in completed]
            primary_metrics = completed[0][2] if completed else None
            deterministic = len(
                {item["result_fingerprint"] for item in repetitions}
            ) == 1 and len({item["metrics_fingerprint"] for item in repetitions}) == 1
            if not deterministic or primary_metrics is None:
                raise RuntimeError(f"Determinism check failed for {window_id}/{action_id}")
            metrics_path = window_root / f"{action_id}-metrics.json"
            _write_json(metrics_path, primary_metrics)
            metrics_paths.append(metrics_path)
            determinism[action_id] = {
                "repetitions": repetitions,
                "fingerprints_match": True,
            }
            policy_records.append(
                {
                    "action_id": action_id,
                    "policy_fingerprint": primary_metrics["policy_fingerprint"],
                    "result_fingerprint": repetitions[0]["result_fingerprint"],
                    "metrics_fingerprint": primary_metrics["metrics_fingerprint"],
                    "deterministic_repetitions": True,
                    "allocation_rate_mean": primary_metrics["cluster"][
                        "allocation_rate_mean"
                    ],
                    "hp_completion_rate": primary_metrics["jobs"]["HP"][
                        "completion_rate"
                    ],
                    "hp_preempted_job_count": primary_metrics["jobs"]["HP"][
                        "preempted_job_count"
                    ],
                    "hp_jct_p95_seconds": primary_metrics["jobs"]["HP"][
                        "jct_seconds"
                    ]["p95"],
                    "hp_queue_p95_seconds": primary_metrics["jobs"]["HP"][
                        "queue_seconds"
                    ]["p95"],
                    "spot_completion_rate": primary_metrics["jobs"]["Spot"][
                        "completion_rate"
                    ],
                    "spot_jct_p95_seconds": primary_metrics["jobs"]["Spot"][
                        "jct_seconds"
                    ]["p95"],
                    "spot_eviction_rate_per_run": primary_metrics[
                        "preemption_events"
                    ]["eviction_rate_per_run"],
                    "spot_guarantee_success_rate": primary_metrics["spot_guarantee"][
                        "success_rate"
                    ],
                }
            )
        _write_json(window_root / "determinism.json", determinism)

        portfolio = compare_policy_portfolio(metrics_paths)
        _write_json(window_root / "policy-portfolio.json", portfolio)
        baseline_path = next(
            path for path in metrics_paths if path.name == "native-fifo-metrics.json"
        )
        audit_paths: list[Path] = []
        for policy_record, metrics_path in zip(policy_records, metrics_paths):
            audit = audit_slo(metrics_path, slo_path, baseline_path)
            audit_path = window_root / f"{policy_record['action_id']}-slo-audit.json"
            _write_json(audit_path, audit)
            audit_paths.append(audit_path)
            policy_record["audit_fingerprint"] = audit["audit_fingerprint"]
            policy_record["hard_slo_passed"] = audit["audit_passed"]
            policy_record["allocation_soft_target_met"] = _soft_target(audit)

        ranking = rank_audited_policies(metrics_paths, audit_paths, slo_path)
        _write_json(window_root / "policy-ranking.json", ranking)
        action_by_fingerprint = {
            item["policy_fingerprint"]: item["action_id"] for item in policy_records
        }
        record: dict[str, Any] = {
            "window_id": window_id,
            "date": date,
            "stratum": selected["stratum"],
            "window_seconds": {
                "warmup_start": warmup_start,
                "start": start,
                "end": end,
            },
            "trace_fingerprint": trace.fingerprint,
            "workload_fingerprint": workload["workload_fingerprint"],
            "population": {
                "HP": workload["population"]["HP"]["job_count"],
                "Spot": workload["population"]["Spot"]["job_count"],
            },
            "regime_signals": workload["regime_signals"],
            "policies": policy_records,
            "portfolio_fingerprint": portfolio["portfolio_fingerprint"],
            "ranking": {
                "selection_status": ranking["selection_status"],
                "selected_action_ids": [
                    action_by_fingerprint[fingerprint]
                    for fingerprint in ranking["selected_policy_fingerprints"]
                ],
                "ranking_fingerprint": ranking["ranking_fingerprint"],
            },
        }
        record["window_record_fingerprint"] = canonical_sha256(record)
        _write_json(window_root / "window-record.json", record)
        records.append(record)

    aggregate = aggregate_multiwindow_records(
        records,
        selection_fingerprint=selection["selection_fingerprint"],
    )
    _write_json(output_root / "multiwindow-summary.json", aggregate)
    manifest: dict[str, Any] = {
        "schema_version": "schednav.multiwindow-experiment/v1",
        "source": selection_trace.source,
        "action_space": {
            "path": args.action_space,
            "name": action_space["name"],
            "fingerprint": canonical_sha256(action_space),
            "excluded_profiles": action_space.get("excluded_profiles", []),
        },
        "selection_fingerprint": selection["selection_fingerprint"],
        "selected_window_count": selection["selected_window_count"],
        "policy_action_ids": [
            json.loads(path.read_text(encoding="utf-8"))["action_id"]
            for path in policy_paths
        ],
        "repetitions_per_policy_per_window": args.repetitions,
        "parallel_workers": args.workers,
        "warmup_seconds": args.warmup_seconds,
        "multiwindow_fingerprint": aggregate["multiwindow_fingerprint"],
    }
    manifest["experiment_fingerprint"] = canonical_sha256(manifest)
    _write_json(output_root / "experiment-manifest.json", manifest)
    print(
        json.dumps(
            {
                "output_directory": str(output_root),
                "selected_window_count": selection["selected_window_count"],
                "selection_fingerprint": selection["selection_fingerprint"],
                "multiwindow_fingerprint": aggregate["multiwindow_fingerprint"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
