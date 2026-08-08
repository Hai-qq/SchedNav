"""Filesystem and subprocess adapter around the compatibility-patched GFS scheduler."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from .contracts import RunSpec, canonical_sha256


REPLICATE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REQUIRED_GFS_OPTIONS = (
    "--trace-start",
    "--trace-end",
    "--log-start",
    "--log-end",
    "--embed",
    "--seed",
    "--deterministic",
    "--cpu",
)
GFS_VENDOR_MANIFEST = "SCHEDNAV_GFS_VENDOR_MANIFEST.json"
TRACE_VENDOR_MANIFEST = "SCHEDNAV_TRACE_VENDOR_MANIFEST.json"
GFS_VENDOR_SCHEMA = "schednav.gfs-vendor-manifest/v1"
TRACE_VENDOR_SCHEMA = "schednav.trace-vendor-manifest/v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve(project_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (project_root / path).resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _verify_vendored_input(
    root: Path,
    manifest_name: str,
    expected_schema: str,
    expected_commit: str,
) -> dict[str, Any]:
    manifest_path = root / manifest_name
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid vendored input manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != expected_schema:
        raise ValueError(f"Expected schema_version={expected_schema} in {manifest_path}")
    if manifest.get("upstream_commit") != expected_commit:
        raise ValueError(
            f"Vendored input commit mismatch: expected {expected_commit}, got {manifest.get('upstream_commit')}"
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError(f"Vendored input manifest has no attested files: {manifest_path}")
    root = root.resolve()
    for relative, metadata in files.items():
        if not isinstance(relative, str) or not isinstance(metadata, dict):
            raise ValueError(f"Invalid vendored file entry in {manifest_path}")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"Unsafe vendored file path: {relative!r}")
        path = (root / relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Vendored file escapes its root: {relative!r}") from exc
        if not path.is_file():
            raise ValueError(f"Vendored file is missing: {relative!r}")
        expected_sha256 = metadata.get("sha256")
        expected_size = metadata.get("size_bytes")
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError(f"Vendored file has an invalid SHA-256: {relative!r}")
        if sha256_file(path) != expected_sha256 or path.stat().st_size != expected_size:
            raise ValueError(f"Vendored file failed attestation: {relative!r}")
    return manifest


def verify_local_inputs(spec: RunSpec, project_root: Path) -> dict[str, Path]:
    paths = {
        "gfs_dir": _resolve(project_root, spec.gfs_dir),
        "gfs_patch": _resolve(project_root, spec.gfs_patch),
        "source_trace_dir": _resolve(project_root, spec.source_trace_dir),
        "python_executable": _resolve(project_root, spec.python_executable),
        "artifacts_dir": _resolve(project_root, spec.artifacts_dir),
    }
    required = (
        paths["gfs_dir"] / "simulator.py",
        paths["gfs_dir"] / "requirements.txt",
        paths["gfs_patch"],
        paths["source_trace_dir"] / "node_info_df.csv",
        paths["source_trace_dir"] / "job_info_df.csv",
        paths["python_executable"],
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing reproduction inputs: {missing}")
    gfs_vendor_path = paths["gfs_dir"] / GFS_VENDOR_MANIFEST
    if gfs_vendor_path.is_file():
        gfs_vendor = _verify_vendored_input(
            paths["gfs_dir"], GFS_VENDOR_MANIFEST, GFS_VENDOR_SCHEMA, spec.gfs_commit
        )
        if gfs_vendor.get("compatibility_patch_sha256") != sha256_file(paths["gfs_patch"]):
            raise ValueError("Vendored GFS compatibility patch hash mismatch")
    else:
        actual_gfs_commit = _git_commit(paths["gfs_dir"])
        if actual_gfs_commit != spec.gfs_commit:
            raise ValueError(f"GFS commit mismatch: expected {spec.gfs_commit}, got {actual_gfs_commit}")
        expected_modified_files = {
            "estimator/gpu_request_estimator.py",
            "estimator/utils/losses.py",
            "policy/fifo_spot.py",
            "policy/policy.py",
            "policy/recorder/recorder.py",
            "policy/spot.py",
            "requirements.txt",
            "simulator.py",
        }
        diff_result = subprocess.run(
            ["git", "-C", str(paths["gfs_dir"]), "diff", "--name-only"],
            capture_output=True,
            check=True,
            text=True,
        )
        modified_files = {
            line.strip().replace("\\", "/") for line in diff_result.stdout.splitlines() if line.strip()
        }
        if modified_files != expected_modified_files:
            raise ValueError(
                f"GFS tracked modifications must exactly match the compatibility patch paths; got {sorted(modified_files)}"
            )
        reverse_check = subprocess.run(
            ["git", "-C", str(paths["gfs_dir"]), "apply", "--reverse", "--check", str(paths["gfs_patch"])],
            capture_output=True,
            check=False,
            text=True,
        )
        if reverse_check.returncode != 0:
            raise ValueError("The configured GFS compatibility patch does not match the local GFS worktree")
    trace_vendor_path = paths["source_trace_dir"] / TRACE_VENDOR_MANIFEST
    if trace_vendor_path.is_file():
        _verify_vendored_input(
            paths["source_trace_dir"], TRACE_VENDOR_MANIFEST, TRACE_VENDOR_SCHEMA, spec.trace_commit
        )
    else:
        actual_trace_commit = _git_commit(paths["source_trace_dir"])
        if actual_trace_commit != spec.trace_commit:
            raise ValueError(f"Trace commit mismatch: expected {spec.trace_commit}, got {actual_trace_commit}")
    simulator_text = (paths["gfs_dir"] / "simulator.py").read_text(encoding="utf-8")
    missing_options = [option for option in REQUIRED_GFS_OPTIONS if option not in simulator_text]
    if missing_options:
        raise ValueError(f"GFS compatibility patch is missing options: {missing_options}")
    return paths


def _prepared_manifest_valid(manifest: dict[str, Any], prepared_dir: Path) -> bool:
    for name, metadata in manifest.get("prepared_files", {}).items():
        path = prepared_dir / name
        if not path.exists() or sha256_file(path) != metadata.get("sha256"):
            return False
    return True


def prepare_trace(spec: RunSpec, project_root: Path) -> tuple[Path, dict[str, Any]]:
    paths = verify_local_inputs(spec, project_root)
    source_dir = paths["source_trace_dir"]
    source_files = {
        "node_info_df.csv": sha256_file(source_dir / "node_info_df.csv"),
        "job_info_df.csv": sha256_file(source_dir / "job_info_df.csv"),
    }
    identity = {
        "source_commit": spec.trace_commit,
        "source_files": source_files,
        "gpu_models": spec.window.gpu_models,
        "trace_end_seconds": spec.window.seconds_from_origin(spec.window.trace_end),
        "evaluation_start_seconds": spec.window.seconds_from_origin(spec.window.evaluation_start),
        "evaluation_end_seconds": spec.window.seconds_from_origin(spec.window.evaluation_end),
    }
    trace_id = canonical_sha256(identity)[:16]
    prepared_dir = paths["artifacts_dir"] / "prepared" / trace_id
    manifest_path = prepared_dir / "trace_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not _prepared_manifest_valid(manifest, prepared_dir):
            raise ValueError(f"Prepared trace failed hash verification: {prepared_dir}")
        return prepared_dir, manifest
    if prepared_dir.exists():
        raise FileExistsError(f"Incomplete prepared trace already exists: {prepared_dir}")
    prepared_dir.mkdir(parents=True, exist_ok=False)

    gpu_models = set(spec.window.gpu_models)
    counts = {"nodes": 0, "hp_jobs": 0, "spot_jobs": 0}
    node_target = prepared_dir / "node_info_df.csv"
    with (source_dir / "node_info_df.csv").open("r", encoding="utf-8", newline="") as source, node_target.open(
        "w", encoding="utf-8", newline=""
    ) as target:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(target, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            if row["gpu_model"] in gpu_models:
                writer.writerow(row)
                counts["nodes"] += 1

    trace_end = spec.window.seconds_from_origin(spec.window.trace_end)
    evaluation_start = spec.window.seconds_from_origin(spec.window.evaluation_start)
    evaluation_end = spec.window.seconds_from_origin(spec.window.evaluation_end)
    job_target = prepared_dir / "job_info_df.csv"
    with (source_dir / "job_info_df.csv").open("r", encoding="utf-8", newline="") as source, job_target.open(
        "w", encoding="utf-8", newline=""
    ) as target:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(target, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            if row["gpu_model"] not in gpu_models:
                continue
            submit_time = float(row["submit_time"])
            job_type = row["job_type"]
            keep_hp = job_type == "HP" and 0 <= submit_time <= trace_end
            keep_spot = job_type == "Spot" and evaluation_start <= submit_time <= evaluation_end
            if keep_hp or keep_spot:
                writer.writerow(row)
                counts["hp_jobs" if keep_hp else "spot_jobs"] += 1

    if counts["nodes"] == 0 or counts["hp_jobs"] == 0 or counts["spot_jobs"] == 0:
        raise ValueError(f"Golden trace must contain nodes, HP jobs, and Spot jobs; got {counts}")
    prepared_files = {
        path.name: {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}
        for path in (node_target, job_target)
    }
    manifest = {
        "schema_version": "schednav.trace-manifest/v1",
        "trace_id": trace_id,
        "created_at": _utc_now(),
        "identity": identity,
        "counts": counts,
        "prepared_files": prepared_files,
    }
    _write_json(manifest_path, manifest)
    return prepared_dir, manifest


def _build_command(
    spec: RunSpec,
    paths: dict[str, Path],
    prepared_dir: Path,
    experiment_name: str,
    run_dir: Path,
) -> list[str]:
    policy = spec.policy
    command = [
        str(paths["python_executable"]),
        str(paths["gfs_dir"] / "simulator.py"),
        "--experiment-name",
        experiment_name,
        "--trace-dir",
        str(prepared_dir),
        "--log-dir",
        str(run_dir / "gfs-output"),
        "--scheduler",
        policy.scheduler,
        "--trace-start",
        spec.window.submit_time_origin,
        "--trace-end",
        spec.window.trace_end,
        "--log-start",
        spec.window.evaluation_start,
        "--log-end",
        spec.window.evaluation_end,
        "--guarantee_hour",
        *(str(value) for value in policy.guarantee_hours),
        "--guarantee_rate",
        str(policy.guarantee_rate),
        "--ckpt_interval",
        str(policy.ckpt_interval_seconds),
        "--seq_len",
        str(policy.seq_len_hours),
        "--pred_len",
        str(policy.pred_len_hours),
        "--train_epochs",
        str(policy.train_epochs),
        "--num_workers",
        str(policy.num_workers),
        "--seed",
        str(policy.seed),
        "--embed",
        "timeF",
        "--checkpoints",
        str(run_dir / "checkpoints"),
    ]
    if policy.deterministic:
        command.append("--deterministic")
    if policy.device == "cpu":
        command.append("--cpu")
    return command


def _collect_result_files(run_dir: Path, experiment_name: str) -> list[dict[str, Any]]:
    base = run_dir / "gfs-output" / experiment_name
    if not base.exists():
        return []
    return [
        {
            "path": path.relative_to(base).as_posix(),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(base.rglob("*.csv"))
    ]


def run_reproduction(spec: RunSpec, project_root: Path, replicate: str) -> tuple[Path, dict[str, Any]]:
    if not REPLICATE_PATTERN.fullmatch(replicate):
        raise ValueError("replicate must be path-safe and contain only letters, numbers, dot, underscore, or dash")
    paths = verify_local_inputs(spec, project_root)
    prepared_dir, trace_manifest = prepare_trace(spec, project_root)
    experiment_name = f"{spec.experiment_name}-{replicate}"
    run_dir = paths["artifacts_dir"] / "runs" / experiment_name
    run_dir.mkdir(parents=True, exist_ok=False)
    command = _build_command(spec, paths, prepared_dir, experiment_name, run_dir)
    manifest_path = run_dir / "run_manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": "schednav.run-manifest/v1",
        "run_id": experiment_name,
        "status": "running",
        "started_at": _utc_now(),
        "run_spec_fingerprint": spec.fingerprint,
        "policy_fingerprint": spec.policy.fingerprint,
        "gfs_commit": spec.gfs_commit,
        "gfs_patch_sha256": sha256_file(paths["gfs_patch"]),
        "trace_commit": spec.trace_commit,
        "trace_manifest_sha256": canonical_sha256(
            {key: value for key, value in trace_manifest.items() if key != "created_at"}
        ),
        "trace_id": trace_manifest["trace_id"],
        "command": command,
        "result_files": [],
    }
    _write_json(manifest_path, manifest)
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": str(spec.policy.seed),
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
        }
    )
    with (run_dir / "stdout.log").open("w", encoding="utf-8") as stdout, (run_dir / "stderr.log").open(
        "w", encoding="utf-8"
    ) as stderr:
        result = subprocess.run(
            command,
            cwd=paths["gfs_dir"],
            env=environment,
            stdout=stdout,
            stderr=stderr,
            check=False,
            text=True,
        )
    result_files = _collect_result_files(run_dir, experiment_name)
    succeeded = result.returncode == 0 and bool(result_files)
    manifest.update(
        {
            "status": "succeeded" if succeeded else "failed",
            "finished_at": _utc_now(),
            "exit_code": result.returncode,
            "result_files": result_files,
        }
    )
    _write_json(manifest_path, manifest)
    if result.returncode != 0:
        raise RuntimeError(f"GFS run failed; inspect {run_dir / 'stderr.log'}")
    if not result_files:
        raise RuntimeError(f"GFS run succeeded without CSV evidence: {run_dir}")
    return run_dir, manifest


def compare_run_manifests(first_path: Path, second_path: Path) -> dict[str, Any]:
    first = json.loads(first_path.read_text(encoding="utf-8"))
    second = json.loads(second_path.read_text(encoding="utf-8"))
    comparable = all(
        (
            first.get("status") == "succeeded",
            second.get("status") == "succeeded",
            first.get("run_spec_fingerprint") == second.get("run_spec_fingerprint"),
            first.get("policy_fingerprint") == second.get("policy_fingerprint"),
            first.get("trace_id") == second.get("trace_id"),
            bool(first.get("gfs_patch_sha256")),
            first.get("gfs_patch_sha256") == second.get("gfs_patch_sha256"),
        )
    )
    first_files = {item["path"]: item["sha256"] for item in first.get("result_files", [])}
    second_files = {item["path"]: item["sha256"] for item in second.get("result_files", [])}
    differences = {
        path: {"first": first_files.get(path), "second": second_files.get(path)}
        for path in sorted(first_files.keys() | second_files.keys())
        if first_files.get(path) != second_files.get(path)
    }
    return {
        "schema_version": "schednav.reproduction-comparison/v1",
        "comparable": comparable,
        "deterministic_match": comparable and not differences and bool(first_files),
        "first_run": first.get("run_id"),
        "second_run": second.get("run_id"),
        "compared_files": sorted(first_files.keys() | second_files.keys()),
        "differences": differences,
    }
