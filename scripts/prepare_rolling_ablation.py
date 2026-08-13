"""Prepare the frozen rolling-ablation traces from the raw Alibaba tables."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from schednav.contracts import canonical_sha256
from schednav.native_trace import import_alibaba_trace, load_canonical_trace


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _verified(value: dict[str, Any], field: str) -> bool:
    supplied = value.get(field)
    payload = {key: item for key, item in value.items() if key != field}
    return isinstance(supplied, str) and canonical_sha256(payload) == supplied


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _ensure_empty_or_manifest(path: Path) -> None:
    if path.exists() and not path.is_dir():
        raise ValueError(f"Trace target is not a directory: {path}")
    if path.is_dir() and any(path.iterdir()):
        raise ValueError(f"Refusing to overwrite a partial trace directory: {path}")


def _build_data_contract(
    study: dict[str, Any],
    source_hashes: dict[str, str],
    windows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Freeze trace identities without running any scheduling policy."""

    data = study["data"]
    value: dict[str, Any] = {
        "schema_version": "schednav.rolling-ablation-data/v1",
        "study_id": study["study_id"],
        "design_fingerprint": study["design_fingerprint"],
        "dataset": {
            "name": data["dataset"],
            "commit": data["source_commit"],
            **source_hashes,
            "time_origin": data["time_origin"],
            "gpu_model": data["gpu_model"],
        },
        "execution_contract": {
            "warmup_start_seconds": 0,
            "include_warmup_spot": False,
            "purpose": (
                "HP history from source origin plus evaluation-window HP and "
                "Spot arrivals"
            ),
        },
        "workload_history_contract": {
            "lookback_seconds": int(
                study["rolling_contract"]["history_window_seconds"]
            ),
            "include_warmup_spot": True,
            "purpose": (
                "past-only HP and Spot workload signals through each rolling cutoff"
            ),
        },
        "windows": windows,
    }
    value["data_fingerprint"] = canonical_sha256(value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--dataset-directory", required=True)
    parser.add_argument(
        "--study", default="configs/studies/rolling-ablation-v1.json"
    )
    parser.add_argument(
        "--data-contract",
        default="configs/studies/rolling-ablation-data-v1.json",
    )
    parser.add_argument(
        "--output-directory", default="datasets/local/rolling-ablation-v1"
    )
    parser.add_argument(
        "--freeze-data-contract",
        action="store_true",
        help=(
            "Generate canonical traces and atomically create a new content-addressed "
            "data contract. This does not run a scheduling policy."
        ),
    )
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    dataset = Path(args.dataset_directory).resolve()
    output = (root / args.output_directory).resolve()
    study = _load((root / args.study).resolve())
    contract_path = (root / args.data_contract).resolve()
    if args.freeze_data_contract and contract_path.exists():
        raise ValueError(f"Refusing to overwrite frozen data contract: {contract_path}")
    contract = None if args.freeze_data_contract else _load(contract_path)
    if not _verified(study, "design_fingerprint"):
        raise ValueError("Rolling study design fingerprint is invalid")
    if contract is not None and not _verified(contract, "data_fingerprint"):
        raise ValueError("Rolling data-contract fingerprint is invalid")
    if (
        contract is not None
        and contract.get("design_fingerprint") != study["design_fingerprint"]
    ):
        raise ValueError("Rolling data contract belongs to a different design")

    node_info = dataset / "node_info_df.csv"
    job_info = dataset / "job_info_df.csv"
    if not node_info.is_file() or not job_info.is_file():
        raise FileNotFoundError(
            "Dataset directory must contain node_info_df.csv and job_info_df.csv"
        )
    observed_hashes = {
        "node_info_sha256": _sha256(node_info),
        "job_info_sha256": _sha256(job_info),
    }
    if contract is not None:
        expected_hashes = {
            key: contract["dataset"][key]
            for key in ("node_info_sha256", "job_info_sha256")
        }
        if observed_hashes != expected_hashes:
            raise ValueError("Raw dataset hashes differ from the frozen data contract")

    expected_by_window = (
        {item["window_id"]: item for item in contract["windows"]}
        if contract is not None
        else {}
    )
    dataset_contract = contract["dataset"] if contract is not None else {
        "time_origin": study["data"]["time_origin"],
        "commit": study["data"]["source_commit"],
        "gpu_model": study["data"]["gpu_model"],
    }
    execution_warmup_start = (
        float(contract["execution_contract"]["warmup_start_seconds"])
        if contract is not None
        else 0.0
    )
    workload_history_lookback = (
        float(contract["workload_history_contract"]["lookback_seconds"])
        if contract is not None
        else float(study["rolling_contract"]["history_window_seconds"])
    )
    prepared = []
    for index, window in enumerate(study["holdout_windows"], start=1):
        window_id = str(window["window_id"])
        if contract is not None and window_id not in expected_by_window:
            raise ValueError(f"Data contract is missing {window_id}")
        expected = expected_by_window.get(window_id)
        window_root = output / "windows" / window_id
        execution_dir = window_root / "execution"
        history_dir = window_root / "history"
        execution_path = execution_dir / "trace.json"
        history_path = history_dir / "trace.json"
        print(f"[prepare {index}/{len(study['holdout_windows'])}] {window_id}", flush=True)
        if not execution_path.is_file():
            _ensure_empty_or_manifest(execution_dir)
            import_alibaba_trace(
                node_info,
                job_info,
                execution_dir,
                trace_id=f"alibaba-gpu-series-2-rolling-{window_id}",
                time_origin=dataset_contract["time_origin"],
                source_commit=dataset_contract["commit"],
                gpu_models={dataset_contract["gpu_model"]},
                evaluation_start_seconds=float(window["start_seconds"]),
                evaluation_end_seconds=float(window["end_seconds"]),
                warmup_start_seconds=execution_warmup_start,
                include_warmup_spot=False,
            )
        if not history_path.is_file():
            _ensure_empty_or_manifest(history_dir)
            import_alibaba_trace(
                node_info,
                job_info,
                history_dir,
                trace_id=f"alibaba-gpu-series-2-rolling-history-{window_id}",
                time_origin=dataset_contract["time_origin"],
                source_commit=dataset_contract["commit"],
                gpu_models={dataset_contract["gpu_model"]},
                evaluation_start_seconds=float(window["start_seconds"]),
                evaluation_end_seconds=float(window["end_seconds"]),
                warmup_start_seconds=float(window["start_seconds"])
                - workload_history_lookback,
                include_warmup_spot=True,
            )
        execution = load_canonical_trace(execution_path)
        history = load_canonical_trace(history_path)
        observed = {
            "window_id": window_id,
            "execution_trace_fingerprint": execution.fingerprint,
            "history_trace_fingerprint": history.fingerprint,
            "execution_job_count": len(execution.jobs),
            "history_job_count": len(history.jobs),
        }
        if expected is not None and observed != expected:
            raise ValueError(f"Prepared trace differs from frozen contract: {window_id}")
        prepared.append(observed)

    if contract is None:
        contract = _build_data_contract(study, observed_hashes, prepared)
        _write(contract_path, contract)

    manifest: dict[str, Any] = {
        "schema_version": "schednav.rolling-ablation-preparation/v1",
        "design_fingerprint": study["design_fingerprint"],
        "data_fingerprint": contract["data_fingerprint"],
        "source_hashes": observed_hashes,
        "windows": prepared,
    }
    manifest["preparation_fingerprint"] = canonical_sha256(manifest)
    _write(output / "preparation-manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
