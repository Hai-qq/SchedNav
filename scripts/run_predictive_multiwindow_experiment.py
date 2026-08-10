"""Prepare, execute, lock, and publish a cutoff-safe predictive study."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT_DEFAULT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from schednav.contracts import canonical_sha256
from schednav.controller_factory import load_controller_config
from schednav.native_simulator import (
    SimulationPolicy,
    run_native_simulation,
    run_predictive_simulation,
)
from schednav.native_trace import import_alibaba_trace, load_canonical_trace
from schednav.native_workload import analyze_trace_file
from schednav.predictive_multiwindow import (
    RUN_RECEIPT_SCHEMA,
    build_arm_record,
    build_partition_summary,
    build_public_evidence,
    build_selection_lock,
    build_window_record,
    load_predictive_multiwindow_study,
    verify_selection_lock,
)
from schednav.slo import audit_slo


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _verified(value: dict[str, Any], key: str) -> bool:
    supplied = value.get(key)
    payload = {name: item for name, item in value.items() if name != key}
    return isinstance(supplied, str) and canonical_sha256(payload) == supplied


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Project-relative path escapes the project: {relative}") from exc
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def _study_context(
    project_root: Path, study_relative: str
) -> tuple[Path, dict[str, Any]]:
    study_path = _resolve_inside(project_root, study_relative)
    study = load_predictive_multiwindow_study(study_path)
    prior = _load_json(
        _resolve_inside(project_root, study["prior_window_selection"]["receipt"])
    )
    observed = prior.get("source_evidence", {}).get("selection_fingerprint")
    if observed != study["prior_window_selection"]["selection_fingerprint"]:
        raise ValueError("Frozen predictive windows do not match the prior selection receipt")
    return study_path, study


def _initialize_output(output_root: Path, study: dict[str, Any]) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    frozen_path = output_root / "study-design.json"
    if frozen_path.exists():
        if _load_json(frozen_path) != study:
            raise ValueError("Output directory belongs to a different study design")
    else:
        _write_json(frozen_path, study)


def _window_root(output_root: Path, window: dict[str, Any]) -> Path:
    return output_root / "windows" / str(window["date"])


def _trace_path(output_root: Path, window: dict[str, Any]) -> Path:
    return _window_root(output_root, window) / "trace" / "trace.json"


def _validate_prepared_trace(
    study: dict[str, Any], window: dict[str, Any], trace_path: Path
) -> None:
    trace = load_canonical_trace(trace_path)
    if trace.schema_version != "schednav.trace/v2":
        raise ValueError(f"Prepared trace is not trace/v2: {window['window_id']}")
    if trace.evaluation_start_seconds != float(window["start_seconds"]) or (
        trace.evaluation_end_seconds != float(window["end_seconds"])
    ):
        raise ValueError(f"Prepared trace window differs from design: {window['window_id']}")
    source_filter = trace.source.get("filter", {})
    if source_filter.get("include_warmup_spot") is not False:
        raise ValueError(f"Prepared trace includes warmup Spot jobs: {window['window_id']}")
    if source_filter.get("warmup_start_seconds") != float(
        study["trace_contract"]["warmup_start_seconds"]
    ):
        raise ValueError(f"Prepared trace warmup differs from design: {window['window_id']}")
    if trace.source.get("node_info_sha256") != study["dataset"]["node_info_sha256"] or (
        trace.source.get("job_info_sha256") != study["dataset"]["job_info_sha256"]
    ):
        raise ValueError(f"Prepared trace source hashes differ: {window['window_id']}")
    if any(not job.tenant_id for job in trace.jobs):
        raise ValueError(f"Prepared trace has empty tenant IDs: {window['window_id']}")


def prepare(
    project_root: Path,
    study: dict[str, Any],
    dataset_root: Path,
    output_root: Path,
) -> None:
    _initialize_output(output_root, study)
    node_info = dataset_root / "node_info_df.csv"
    job_info = dataset_root / "job_info_df.csv"
    if not node_info.is_file() or not job_info.is_file():
        raise FileNotFoundError(
            "Dataset directory must contain node_info_df.csv and job_info_df.csv"
        )
    observed_hashes = {
        "node_info_sha256": _sha256_file(node_info),
        "job_info_sha256": _sha256_file(job_info),
    }
    expected_hashes = {
        key: study["dataset"][key]
        for key in ("node_info_sha256", "job_info_sha256")
    }
    if observed_hashes != expected_hashes:
        raise ValueError("Dataset hashes differ from the frozen study")

    for ordinal, window in enumerate(study["windows"], start=1):
        window_root = _window_root(output_root, window)
        trace_path = _trace_path(output_root, window)
        if not trace_path.is_file():
            trace_dir = trace_path.parent
            if trace_dir.exists() and any(trace_dir.iterdir()):
                raise ValueError(f"Refusing a partial trace directory: {trace_dir}")
            print(
                f"[prepare {ordinal}/{len(study['windows'])}] {window['window_id']}",
                flush=True,
            )
            import_alibaba_trace(
                node_info,
                job_info,
                trace_dir,
                trace_id=f"alibaba-gpu-series-2-tenant-{window['date']}",
                time_origin=study["dataset"]["time_origin"],
                source_commit=study["dataset"]["commit"],
                gpu_models={study["dataset"]["gpu_model"]},
                evaluation_start_seconds=float(window["start_seconds"]),
                evaluation_end_seconds=float(window["end_seconds"]),
                warmup_start_seconds=float(
                    study["trace_contract"]["warmup_start_seconds"]
                ),
                include_warmup_spot=False,
            )
        _validate_prepared_trace(study, window, trace_path)
        workload_path = window_root / "workload-summary.json"
        workload = analyze_trace_file(trace_path, sample_interval_seconds=3600)
        _write_json(workload_path, workload)

    manifest: dict[str, Any] = {
        "schema_version": "schednav.predictive-multiwindow-preparation/v1",
        "design_fingerprint": study["design_fingerprint"],
        "source_hashes": observed_hashes,
        "window_count": len(study["windows"]),
        "windows": [
            {
                "window_id": window["window_id"],
                "partition": window["partition"],
                "trace_fingerprint": load_canonical_trace(
                    _trace_path(output_root, window)
                ).fingerprint,
                "workload_fingerprint": _load_json(
                    _window_root(output_root, window) / "workload-summary.json"
                )["workload_fingerprint"],
            }
            for window in study["windows"]
        ],
    }
    manifest["preparation_fingerprint"] = canonical_sha256(manifest)
    _write_json(output_root / "preparation-manifest.json", manifest)
    print(
        json.dumps(
            {
                "prepared_window_count": len(study["windows"]),
                "preparation_fingerprint": manifest["preparation_fingerprint"],
            },
            sort_keys=True,
        ),
        flush=True,
    )


def _arm_runtime(
    project_root: Path, arm: dict[str, Any]
) -> dict[str, Any]:
    policy_path = _resolve_inside(project_root, arm["policy"])
    policy = SimulationPolicy.load(policy_path)
    controller_path: Path | None = None
    controller_fingerprint: str | None = None
    if arm["controller"] is not None:
        controller_path = _resolve_inside(project_root, arm["controller"])
        controller_fingerprint = load_controller_config(controller_path).fingerprint
    if arm["arm_id"] == "fifo" and policy.scheduler != "fifo":
        raise ValueError("The fifo study arm must reference a FIFO policy")
    if policy.placement_strategy != "deterministic_best_fit":
        raise ValueError("Study policies must keep deterministic_best_fit placement")
    return {
        "arm_id": arm["arm_id"],
        "kind": arm["kind"],
        "policy_path": str(policy_path),
        "policy_fingerprint": policy.fingerprint,
        "controller_path": str(controller_path) if controller_path else None,
        "controller_fingerprint": controller_fingerprint,
    }


def _run_paths(
    output_root: Path, window: dict[str, Any], arm_id: str, repetition: int
) -> tuple[Path, Path]:
    root = (
        _window_root(output_root, window)
        / "runs"
        / arm_id
        / f"rep-{repetition:02d}"
    )
    return root / "run-receipt.json", root / "metrics.json"


def _execute_task(task: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    trace_path = Path(task["trace_path"])
    policy_path = Path(task["policy_path"])
    if task["kind"] == "predictive":
        result, metrics = run_predictive_simulation(
            trace_path, policy_path, Path(task["controller_path"])
        )
    else:
        result, metrics = run_native_simulation(trace_path, policy_path)
    receipt: dict[str, Any] = {
        "schema_version": RUN_RECEIPT_SCHEMA,
        "design_fingerprint": task["design_fingerprint"],
        "window_id": task["window_id"],
        "partition": task["partition"],
        "trace_fingerprint": task["trace_fingerprint"],
        "arm_id": task["arm_id"],
        "kind": task["kind"],
        "repetition": task["repetition"],
        "policy_fingerprint": task["policy_fingerprint"],
        "controller_fingerprint": task["controller_fingerprint"],
        "result_fingerprint": result["result_fingerprint"],
        "metrics_fingerprint": metrics["metrics_fingerprint"],
        "future_arrivals_visible_to_controller": False,
    }
    receipt["run_receipt_fingerprint"] = canonical_sha256(receipt)
    return receipt, metrics


def _load_completed(
    receipt_path: Path,
    metrics_path: Path,
    task: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not receipt_path.exists() and not metrics_path.exists():
        return None
    if not receipt_path.is_file() or not metrics_path.is_file():
        raise ValueError(f"Partial run artifact cannot be resumed: {receipt_path.parent}")
    receipt = _load_json(receipt_path)
    metrics = _load_json(metrics_path)
    if receipt.get("schema_version") != RUN_RECEIPT_SCHEMA or not _verified(
        receipt, "run_receipt_fingerprint"
    ):
        raise ValueError(f"Invalid run receipt: {receipt_path}")
    for field in (
        "design_fingerprint",
        "window_id",
        "partition",
        "trace_fingerprint",
        "arm_id",
        "kind",
        "repetition",
        "policy_fingerprint",
        "controller_fingerprint",
    ):
        if receipt.get(field) != task.get(field):
            raise ValueError(f"Run receipt field {field} differs: {receipt_path}")
    if not _verified(metrics, "metrics_fingerprint") or metrics.get(
        "metrics_fingerprint"
    ) != receipt.get("metrics_fingerprint"):
        raise ValueError(f"Run metrics fingerprint is invalid: {metrics_path}")
    if metrics.get("source", {}).get("trace_fingerprint") != task["trace_fingerprint"]:
        raise ValueError(f"Run metrics use another trace: {metrics_path}")
    if metrics.get("evidence", {}).get(
        "simulation_result_fingerprint"
    ) != receipt.get("result_fingerprint"):
        raise ValueError(f"Run metrics reference another result: {metrics_path}")
    return receipt, metrics


def _partition_tasks(
    project_root: Path,
    study: dict[str, Any],
    output_root: Path,
    partition: str,
) -> list[tuple[dict[str, Any], Path, Path]]:
    repetitions = int(study["execution"]["repetitions_per_arm_per_window"])
    runtimes = {
        arm["arm_id"]: _arm_runtime(project_root, arm) for arm in study["arms"]
    }
    tasks = []
    for window in study["windows"]:
        if window["partition"] != partition:
            continue
        trace_path = _trace_path(output_root, window)
        _validate_prepared_trace(study, window, trace_path)
        trace = load_canonical_trace(trace_path)
        for arm in study["arms"]:
            runtime = runtimes[arm["arm_id"]]
            for repetition in range(1, repetitions + 1):
                task = {
                    "design_fingerprint": study["design_fingerprint"],
                    "window_id": window["window_id"],
                    "partition": partition,
                    "trace_path": str(trace_path),
                    "trace_fingerprint": trace.fingerprint,
                    "arm_id": runtime["arm_id"],
                    "kind": runtime["kind"],
                    "repetition": repetition,
                    "policy_path": runtime["policy_path"],
                    "policy_fingerprint": runtime["policy_fingerprint"],
                    "controller_path": runtime["controller_path"],
                    "controller_fingerprint": runtime["controller_fingerprint"],
                }
                receipt_path, metrics_path = _run_paths(
                    output_root, window, arm["arm_id"], repetition
                )
                tasks.append((task, receipt_path, metrics_path))
    return tasks


def _build_partition_outputs(
    project_root: Path,
    study: dict[str, Any],
    output_root: Path,
    partition: str,
) -> dict[str, Any]:
    slo_path = _resolve_inside(project_root, study["execution"]["slo"])
    slo = _load_json(slo_path)
    allocation_tie_band = float(slo["ranking"]["allocation_tie_band"])
    repetitions = int(study["execution"]["repetitions_per_arm_per_window"])
    records = []
    for window in study["windows"]:
        if window["partition"] != partition:
            continue
        window_root = _window_root(output_root, window)
        baseline_path = _run_paths(output_root, window, "fifo", 1)[1]
        arm_records = []
        for arm in study["arms"]:
            receipts = []
            primary_metrics: dict[str, Any] | None = None
            primary_metrics_path: Path | None = None
            for repetition in range(1, repetitions + 1):
                receipt_path, metrics_path = _run_paths(
                    output_root, window, arm["arm_id"], repetition
                )
                receipt = _load_json(receipt_path)
                metrics = _load_json(metrics_path)
                receipts.append(receipt)
                if repetition == 1:
                    primary_metrics = metrics
                    primary_metrics_path = metrics_path
            assert primary_metrics is not None and primary_metrics_path is not None
            audit = audit_slo(primary_metrics_path, slo_path, baseline_path)
            _write_json(window_root / "audits" / f"{arm['arm_id']}.json", audit)
            arm_records.append(
                build_arm_record(arm, receipts, primary_metrics, audit)
            )
        trace = load_canonical_trace(_trace_path(output_root, window))
        workload = _load_json(window_root / "workload-summary.json")
        record = build_window_record(
            study, window, trace.fingerprint, workload, arm_records
        )
        _write_json(window_root / "window-record.json", record)
        records.append(record)
    summary = build_partition_summary(
        study,
        partition,
        records,
        allocation_tie_band=allocation_tie_band,
    )
    _write_json(output_root / f"{partition}-summary.json", summary)
    return summary


def run_partition(
    project_root: Path,
    study: dict[str, Any],
    output_root: Path,
    partition: str,
    *,
    workers: int,
) -> dict[str, Any]:
    if workers <= 0:
        raise ValueError("workers must be positive")
    _initialize_output(output_root, study)
    preparation = output_root / "preparation-manifest.json"
    if not preparation.is_file():
        raise FileNotFoundError("Run prepare before executing a partition")
    lock_path = output_root / "selection-lock.json"
    if partition == "holdout":
        if not lock_path.is_file():
            raise FileNotFoundError("Freeze calibration selection before holdout execution")
        verify_selection_lock(study, _load_json(lock_path))

    tasks = _partition_tasks(project_root, study, output_root, partition)
    pending: list[tuple[dict[str, Any], Path, Path]] = []
    for task, receipt_path, metrics_path in tasks:
        if _load_completed(receipt_path, metrics_path, task) is None:
            pending.append((task, receipt_path, metrics_path))
    if partition == "calibration" and lock_path.exists() and pending:
        raise ValueError("Calibration is locked; missing runs cannot be added afterward")

    if pending:
        print(
            f"[{partition}] pending={len(pending)} total={len(tasks)} workers={workers}",
            flush=True,
        )
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_execute_task, task): (task, receipt_path, metrics_path)
                for task, receipt_path, metrics_path in pending
            }
            completed = len(tasks) - len(pending)
            for future in as_completed(futures):
                task, receipt_path, metrics_path = futures[future]
                receipt, metrics = future.result()
                _write_json(metrics_path, metrics)
                _write_json(receipt_path, receipt)
                completed += 1
                print(
                    f"[{partition} {completed}/{len(tasks)}] "
                    f"{task['window_id']} {task['arm_id']} rep-{task['repetition']:02d}",
                    flush=True,
                )
    else:
        print(f"[{partition}] all {len(tasks)} runs resumed", flush=True)

    summary = _build_partition_outputs(
        project_root, study, output_root, partition
    )
    print(
        json.dumps(
            {
                "partition": partition,
                "window_count": summary["window_count"],
                "partition_summary_fingerprint": summary[
                    "partition_summary_fingerprint"
                ],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return summary


def freeze(study: dict[str, Any], output_root: Path) -> dict[str, Any]:
    calibration_path = output_root / "calibration-summary.json"
    if not calibration_path.is_file():
        raise FileNotFoundError("Complete calibration before freezing selection")
    lock_path = output_root / "selection-lock.json"
    calibration = _load_json(calibration_path)
    value = build_selection_lock(study, calibration)
    if lock_path.exists():
        existing = _load_json(lock_path)
        verify_selection_lock(study, existing)
        if existing != value:
            raise ValueError("Existing selection lock differs from calibration")
        print(json.dumps(existing, sort_keys=True), flush=True)
        return existing

    holdout_windows = [
        window for window in study["windows"] if window["partition"] == "holdout"
    ]
    holdout_artifacts = [
        _window_root(output_root, window) / "runs" for window in holdout_windows
    ]
    if any(path.exists() for path in holdout_artifacts) or (
        output_root / "holdout-summary.json"
    ).exists():
        raise ValueError("Holdout results already exist; refusing to create a retrospective lock")
    _write_json(lock_path, value)
    print(json.dumps(value, sort_keys=True), flush=True)
    return value


def publish(
    study: dict[str, Any], output_root: Path, evidence_path: Path
) -> dict[str, Any]:
    calibration = _load_json(output_root / "calibration-summary.json")
    selection_lock = _load_json(output_root / "selection-lock.json")
    holdout = _load_json(output_root / "holdout-summary.json")
    evidence = build_public_evidence(
        study, calibration, selection_lock, holdout
    )
    _write_json(evidence_path, evidence)
    print(
        json.dumps(
            {
                "status": evidence["status"],
                "evidence": str(evidence_path),
                "receipt_fingerprint": evidence["receipt_fingerprint"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(PROJECT_ROOT_DEFAULT))
    parser.add_argument(
        "--study", default="configs/studies/predictive-multiwindow-v1.json"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--dataset-directory", required=True)
    prepare_parser.add_argument("--output-directory", required=True)

    for name in ("run-calibration", "run-holdout"):
        run_parser = subparsers.add_parser(name)
        run_parser.add_argument("--output-directory", required=True)
        run_parser.add_argument("--workers", type=int, default=1)

    freeze_parser = subparsers.add_parser("freeze-selection")
    freeze_parser.add_argument("--output-directory", required=True)

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--output-directory", required=True)
    publish_parser.add_argument("--evidence-output", required=True)

    all_parser = subparsers.add_parser("all")
    all_parser.add_argument("--dataset-directory", required=True)
    all_parser.add_argument("--output-directory", required=True)
    all_parser.add_argument("--evidence-output", required=True)
    all_parser.add_argument("--workers", type=int, default=1)
    return parser


def main() -> int:
    args = _parser().parse_args()
    project_root = Path(args.project_root).resolve()
    _study_path, study = _study_context(project_root, args.study)
    output_root = Path(args.output_directory).resolve()

    if args.command == "prepare":
        prepare(project_root, study, Path(args.dataset_directory).resolve(), output_root)
    elif args.command == "run-calibration":
        run_partition(
            project_root, study, output_root, "calibration", workers=args.workers
        )
    elif args.command == "freeze-selection":
        freeze(study, output_root)
    elif args.command == "run-holdout":
        run_partition(project_root, study, output_root, "holdout", workers=args.workers)
    elif args.command == "publish":
        publish(study, output_root, Path(args.evidence_output).resolve())
    elif args.command == "all":
        prepare(project_root, study, Path(args.dataset_directory).resolve(), output_root)
        run_partition(
            project_root, study, output_root, "calibration", workers=args.workers
        )
        freeze(study, output_root)
        run_partition(project_root, study, output_root, "holdout", workers=args.workers)
        publish(study, output_root, Path(args.evidence_output).resolve())
    else:  # pragma: no cover - argparse enforces the command set.
        raise AssertionError(args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
