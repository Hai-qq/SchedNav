"""SchedNav-owned canonical trace contract and dataset importers."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from .contracts import canonical_sha256


TRACE_SCHEMA = "schednav.trace/v1"
SERVICE_CLASSES = {"HP", "Spot"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_child(root: Path, raw: Any, field: str) -> Path:
    relative = Path(str(raw))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field} must be a safe relative path")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{field} escapes the trace directory") from exc
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def _positive_int(value: Any, field: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{field} must be positive")
    return parsed


def _non_negative_float(value: Any, field: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise ValueError(f"{field} cannot be negative")
    return parsed


def _positive_float(value: Any, field: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise ValueError(f"{field} must be positive")
    return parsed


@dataclass(frozen=True)
class TraceNode:
    node_id: str
    gpu_model: str
    gpu_count: int


@dataclass(frozen=True)
class TraceJob:
    job_id: str
    submit_time_seconds: float
    duration_seconds: float
    gpu_count: float
    service_class: str
    gpu_model: str


@dataclass(frozen=True)
class CanonicalTrace:
    trace_id: str
    time_origin: str
    source: dict[str, Any]
    nodes: tuple[TraceNode, ...]
    jobs: tuple[TraceJob, ...]
    fingerprint: str
    evaluation_start_seconds: float | None = None
    evaluation_end_seconds: float | None = None

    @property
    def capacity_gpus(self) -> int:
        return sum(node.gpu_count for node in self.nodes)


def load_canonical_trace(manifest_path: Path) -> CanonicalTrace:
    """Load and strictly validate a canonical SchedNav trace."""
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "trace_id",
        "time_origin",
        "source",
        "nodes_file",
        "jobs_file",
        "files",
        "trace_fingerprint",
    }
    optional = {"evaluation_window_seconds"}
    if not required.issubset(manifest) or not set(manifest).issubset(required | optional):
        raise ValueError(
            f"Trace manifest fields must contain {sorted(required)} and only allow {sorted(optional)}"
        )
    if manifest["schema_version"] != TRACE_SCHEMA:
        raise ValueError(f"Expected schema_version={TRACE_SCHEMA}")
    datetime.fromisoformat(str(manifest["time_origin"]))
    trace_id = str(manifest["trace_id"])
    if not trace_id or not isinstance(manifest["source"], dict):
        raise ValueError("trace_id must be non-empty and source must be an object")

    root = manifest_path.parent
    node_path = _safe_child(root, manifest["nodes_file"], "nodes_file")
    job_path = _safe_child(root, manifest["jobs_file"], "jobs_file")
    expected_files = {
        str(manifest["nodes_file"]): _sha256_file(node_path),
        str(manifest["jobs_file"]): _sha256_file(job_path),
    }
    if manifest["files"] != expected_files:
        raise ValueError("Trace file hashes do not match the manifest")

    nodes: list[TraceNode] = []
    node_ids: set[str] = set()
    with node_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["node_id", "gpu_model", "gpu_count"]:
            raise ValueError("nodes.csv has an unsupported header")
        for row in reader:
            node_id = row["node_id"].strip()
            gpu_model = row["gpu_model"].strip()
            if not node_id or not gpu_model or node_id in node_ids:
                raise ValueError("Node IDs must be unique and node fields cannot be empty")
            node_ids.add(node_id)
            nodes.append(TraceNode(node_id, gpu_model, _positive_int(row["gpu_count"], "gpu_count")))
    if not nodes:
        raise ValueError("A trace requires at least one node")

    capacities: dict[str, int] = {}
    for node in nodes:
        capacities[node.gpu_model] = capacities.get(node.gpu_model, 0) + node.gpu_count
    total_capacity = sum(capacities.values())

    jobs: list[TraceJob] = []
    job_ids: set[str] = set()
    with job_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        expected_header = [
            "job_id",
            "submit_time_seconds",
            "duration_seconds",
            "gpu_count",
            "service_class",
            "gpu_model",
        ]
        if reader.fieldnames != expected_header:
            raise ValueError("jobs.csv has an unsupported header")
        for row in reader:
            job_id = row["job_id"].strip()
            service_class = row["service_class"].strip()
            gpu_model = row["gpu_model"].strip()
            gpu_count = _positive_float(row["gpu_count"], "gpu_count")
            duration = float(row["duration_seconds"])
            if not job_id or job_id in job_ids:
                raise ValueError("Job IDs must be non-empty and unique")
            if service_class not in SERVICE_CLASSES:
                raise ValueError(f"Unsupported service_class: {service_class}")
            if not gpu_model:
                raise ValueError("gpu_model cannot be empty")
            eligible_capacity = total_capacity if gpu_model == "*" else capacities.get(gpu_model, 0)
            if gpu_count > eligible_capacity:
                raise ValueError(f"Job {job_id} requests more eligible GPUs than the cluster owns")
            if duration <= 0:
                raise ValueError("duration_seconds must be positive")
            job_ids.add(job_id)
            jobs.append(
                TraceJob(
                    job_id=job_id,
                    submit_time_seconds=_non_negative_float(
                        row["submit_time_seconds"], "submit_time_seconds"
                    ),
                    duration_seconds=duration,
                    gpu_count=gpu_count,
                    service_class=service_class,
                    gpu_model=gpu_model,
                )
            )
    if not jobs:
        raise ValueError("A trace requires at least one job")
    jobs.sort(key=lambda job: (job.submit_time_seconds, job.job_id))

    evaluation_start: float | None = None
    evaluation_end: float | None = None
    if "evaluation_window_seconds" in manifest:
        window = manifest["evaluation_window_seconds"]
        if not isinstance(window, dict) or set(window) != {"start", "end"}:
            raise ValueError("evaluation_window_seconds must contain exactly start and end")
        evaluation_start = _non_negative_float(window["start"], "evaluation window start")
        evaluation_end = _non_negative_float(window["end"], "evaluation window end")
        if evaluation_end <= evaluation_start:
            raise ValueError("Evaluation window end must be greater than its start")
        if jobs[0].submit_time_seconds > evaluation_start:
            raise ValueError("Evaluation window cannot start before the first trace arrival")
        if jobs[-1].submit_time_seconds > evaluation_end:
            raise ValueError("Canonical evaluation traces cannot contain post-window arrivals")
        if not any(
            evaluation_start <= job.submit_time_seconds <= evaluation_end for job in jobs
        ):
            raise ValueError("Evaluation window must contain at least one arrival")

    fingerprint_payload = {
        key: value for key, value in manifest.items() if key != "trace_fingerprint"
    }
    if canonical_sha256(fingerprint_payload) != manifest["trace_fingerprint"]:
        raise ValueError("Trace manifest fingerprint is invalid")
    return CanonicalTrace(
        trace_id=trace_id,
        time_origin=str(manifest["time_origin"]),
        source=dict(manifest["source"]),
        nodes=tuple(nodes),
        jobs=tuple(jobs),
        fingerprint=str(manifest["trace_fingerprint"]),
        evaluation_start_seconds=evaluation_start,
        evaluation_end_seconds=evaluation_end,
    )


def write_canonical_trace(
    output_dir: Path,
    *,
    trace_id: str,
    time_origin: str,
    source: dict[str, Any],
    nodes: list[TraceNode],
    jobs: list[TraceJob],
    evaluation_start_seconds: float | None = None,
    evaluation_end_seconds: float | None = None,
) -> Path:
    """Write canonical CSV files and a content-addressed manifest."""
    output_dir = output_dir.resolve()
    datetime.fromisoformat(time_origin)
    if not trace_id or not nodes or not jobs:
        raise ValueError("trace_id, nodes, and jobs are required")
    if (evaluation_start_seconds is None) != (evaluation_end_seconds is None):
        raise ValueError("Evaluation window start and end must be supplied together")
    if evaluation_start_seconds is not None and evaluation_end_seconds is not None:
        if evaluation_start_seconds < 0 or evaluation_end_seconds <= evaluation_start_seconds:
            raise ValueError("Expected 0 <= evaluation start < evaluation end")
    output_dir.mkdir(parents=True, exist_ok=True)
    node_path = output_dir / "nodes.csv"
    job_path = output_dir / "jobs.csv"
    with node_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["node_id", "gpu_model", "gpu_count"])
        for node in sorted(nodes, key=lambda item: item.node_id):
            writer.writerow([node.node_id, node.gpu_model, node.gpu_count])
    with job_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                "job_id",
                "submit_time_seconds",
                "duration_seconds",
                "gpu_count",
                "service_class",
                "gpu_model",
            ]
        )
        for job in sorted(jobs, key=lambda item: (item.submit_time_seconds, item.job_id)):
            writer.writerow(
                [
                    job.job_id,
                    job.submit_time_seconds,
                    job.duration_seconds,
                    job.gpu_count,
                    job.service_class,
                    job.gpu_model,
                ]
            )
    manifest: dict[str, Any] = {
        "schema_version": TRACE_SCHEMA,
        "trace_id": trace_id,
        "time_origin": time_origin,
        "source": source,
        "nodes_file": node_path.name,
        "jobs_file": job_path.name,
        "files": {
            node_path.name: _sha256_file(node_path),
            job_path.name: _sha256_file(job_path),
        },
    }
    if evaluation_start_seconds is not None and evaluation_end_seconds is not None:
        manifest["evaluation_window_seconds"] = {
            "start": float(evaluation_start_seconds),
            "end": float(evaluation_end_seconds),
        }
    manifest["trace_fingerprint"] = canonical_sha256(manifest)
    manifest_path = output_dir / "trace.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    load_canonical_trace(manifest_path)
    return manifest_path


def slice_canonical_trace(
    trace: CanonicalTrace,
    output_dir: Path,
    *,
    trace_id: str,
    max_submit_time_seconds: float,
    gpu_models: set[str] | None = None,
) -> Path:
    """Create a provenance-preserving prefix slice from an existing canonical trace."""
    if max_submit_time_seconds < 0:
        raise ValueError("max_submit_time_seconds cannot be negative")
    jobs = [
        job
        for job in trace.jobs
        if job.submit_time_seconds <= max_submit_time_seconds
        and (gpu_models is None or job.gpu_model in gpu_models or job.gpu_model == "*")
    ]
    if not jobs:
        raise ValueError("The requested trace slice contains no jobs")
    required_models = {job.gpu_model for job in jobs if job.gpu_model != "*"}
    nodes = [
        node
        for node in trace.nodes
        if not required_models or node.gpu_model in required_models
    ]
    if not nodes:
        raise ValueError("The requested trace slice contains no eligible nodes")
    source = dict(trace.source)
    source["canonical_slice"] = {
        "parent_trace_id": trace.trace_id,
        "parent_trace_fingerprint": trace.fingerprint,
        "max_submit_time_seconds": max_submit_time_seconds,
        "gpu_models": sorted(gpu_models) if gpu_models is not None else None,
        "semantics": "Origin-preserving inclusive arrival prefix; selected jobs drain to completion.",
    }
    sliced_evaluation_start = (
        trace.evaluation_start_seconds
        if trace.evaluation_start_seconds is not None
        and max_submit_time_seconds > trace.evaluation_start_seconds
        else None
    )
    return write_canonical_trace(
        output_dir,
        trace_id=trace_id,
        time_origin=trace.time_origin,
        source=source,
        nodes=nodes,
        jobs=jobs,
        evaluation_start_seconds=sliced_evaluation_start,
        evaluation_end_seconds=(
            min(trace.evaluation_end_seconds, max_submit_time_seconds)
            if sliced_evaluation_start is not None
            and trace.evaluation_end_seconds is not None
            else None
        ),
    )


def _iter_json_array(path: Path, chunk_size: int = 1024 * 1024) -> Iterator[dict[str, Any]]:
    """Stream a top-level JSON array without loading a multi-gigabyte file."""
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as handle:
        buffer = ""
        position = 0
        started = False
        ended = False
        while True:
            chunk = handle.read(chunk_size)
            eof = not chunk
            buffer = buffer[position:] + chunk
            position = 0
            while True:
                while position < len(buffer) and buffer[position].isspace():
                    position += 1
                if not started:
                    if position >= len(buffer):
                        break
                    if buffer[position] != "[":
                        raise ValueError("Expected a top-level JSON array")
                    started = True
                    position += 1
                    continue
                while position < len(buffer) and (
                    buffer[position].isspace() or buffer[position] == ","
                ):
                    position += 1
                if position < len(buffer) and buffer[position] == "]":
                    ended = True
                    position += 1
                    break
                if position >= len(buffer):
                    break
                try:
                    value, end = decoder.raw_decode(buffer, position)
                except json.JSONDecodeError:
                    if eof:
                        raise
                    break
                if not isinstance(value, dict):
                    raise ValueError("Expected every trace record to be a JSON object")
                yield value
                position = end
            if ended:
                if buffer[position:].strip() or handle.read(1):
                    raise ValueError("Unexpected content after the JSON array")
                return
            if eof:
                raise ValueError("Unterminated JSON array")


def _parse_philly_time(value: Any) -> datetime | None:
    if value is None or str(value).strip() in {"", "None"}:
        return None
    return datetime.fromisoformat(str(value).strip())


def import_philly_trace(
    job_log_path: Path,
    machine_list_path: Path,
    output_dir: Path,
    *,
    service_class: str,
    trace_id: str = "microsoft-philly",
    source_commit: str = "29a1b87fa2d9ed80b83c9e3a37f3a88d382b031d",
) -> Path:
    """Convert the public Microsoft Philly schema into the SchedNav trace contract.

    Philly does not contain HP/Spot labels. The caller must explicitly choose the
    service class; the choice is recorded in the output provenance.
    """
    if service_class not in SERVICE_CLASSES:
        raise ValueError(f"service_class must be one of {sorted(SERVICE_CLASSES)}")
    job_log_path = job_log_path.resolve()
    machine_list_path = machine_list_path.resolve()
    if not job_log_path.is_file() or not machine_list_path.is_file():
        raise FileNotFoundError("Philly job log and machine list are required")

    nodes: list[TraceNode] = []
    with machine_list_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise ValueError("Philly machine list is empty")
    start = 1 if rows[0] and rows[0][0].strip().lower() in {"machineid", "machine_id"} else 0
    for row in rows[start:]:
        if len(row) < 2 or not row[0].strip():
            continue
        nodes.append(TraceNode(row[0].strip(), "philly-gpu", _positive_int(row[1], "number of GPUs")))
    if not nodes:
        raise ValueError("Philly machine list contains no usable nodes")

    submitted: list[datetime] = []
    for record in _iter_json_array(job_log_path):
        value = _parse_philly_time(record.get("submitted_time"))
        if value is not None:
            submitted.append(value)
    if not submitted:
        raise ValueError("Philly job log contains no usable submit timestamps")
    origin = min(submitted)

    jobs: list[TraceJob] = []
    skipped = 0
    for record in _iter_json_array(job_log_path):
        submit = _parse_philly_time(record.get("submitted_time"))
        attempts = record.get("attempts")
        if submit is None or not isinstance(attempts, list):
            skipped += 1
            continue
        total_runtime = 0.0
        max_gpus = 0
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            start_time = _parse_philly_time(attempt.get("start_time"))
            end_time = _parse_philly_time(attempt.get("end_time"))
            detail = attempt.get("detail")
            if start_time is None or end_time is None or end_time <= start_time:
                continue
            total_runtime += (end_time - start_time).total_seconds()
            if isinstance(detail, list):
                requested = sum(
                    len(item.get("gpus", []))
                    for item in detail
                    if isinstance(item, dict) and isinstance(item.get("gpus"), list)
                )
                max_gpus = max(max_gpus, requested)
        if total_runtime <= 0 or max_gpus <= 0:
            skipped += 1
            continue
        jobs.append(
            TraceJob(
                job_id=str(record.get("jobid") or f"philly-{len(jobs):08d}"),
                submit_time_seconds=(submit - origin).total_seconds(),
                duration_seconds=total_runtime,
                gpu_count=max_gpus,
                service_class=service_class,
                gpu_model="philly-gpu",
            )
        )
    if not jobs:
        raise ValueError("Philly job log contains no schedulable jobs")
    source = {
        "dataset": "Microsoft Philly GPU Trace",
        "repository": "https://github.com/msr-fiddle/philly-traces",
        "commit": source_commit,
        "license": "CC-BY-4.0",
        "job_log_sha256": _sha256_file(job_log_path),
        "machine_list_sha256": _sha256_file(machine_list_path),
        "service_class_mapping": {
            "mode": "caller_supplied_constant",
            "value": service_class,
            "reason": "The source trace does not publish HP/Spot labels.",
        },
        "skipped_job_count": skipped,
    }
    return write_canonical_trace(
        output_dir,
        trace_id=trace_id,
        time_origin=origin.isoformat(sep=" "),
        source=source,
        nodes=nodes,
        jobs=jobs,
    )


def import_alibaba_trace(
    node_info_path: Path,
    job_info_path: Path,
    output_dir: Path,
    *,
    trace_id: str = "alibaba-spot-gpu",
    time_origin: str = "2024-03-01 00:00:00",
    source_commit: str = "0d0f3f1efdbf1add6a7bcc63676eafbd1eb11f71",
    gpu_models: set[str] | None = None,
    max_submit_time_seconds: float | None = None,
    evaluation_start_seconds: float | None = None,
    evaluation_end_seconds: float | None = None,
) -> Path:
    """Convert Alibaba's published node/job tables into the canonical contract."""
    node_info_path = node_info_path.resolve()
    job_info_path = job_info_path.resolve()
    if not node_info_path.is_file() or not job_info_path.is_file():
        raise FileNotFoundError("Alibaba node_info_df.csv and job_info_df.csv are required")
    datetime.fromisoformat(time_origin)
    if (evaluation_start_seconds is None) != (evaluation_end_seconds is None):
        raise ValueError("Evaluation window start and end must be supplied together")
    if evaluation_start_seconds is not None and evaluation_end_seconds is not None:
        if evaluation_start_seconds < 0 or evaluation_end_seconds <= evaluation_start_seconds:
            raise ValueError("Expected 0 <= evaluation start < evaluation end")
        if (
            max_submit_time_seconds is not None
            and max_submit_time_seconds != evaluation_end_seconds
        ):
            raise ValueError("max_submit_time_seconds must equal the evaluation end")
        max_submit_time_seconds = evaluation_end_seconds

    nodes: list[TraceNode] = []
    with node_info_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"gpu_model", "gpu_capacity_num", "node_name"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("Alibaba node table has an unsupported header")
        for row in reader:
            if gpu_models is not None and row["gpu_model"] not in gpu_models:
                continue
            nodes.append(
                TraceNode(
                    str(row["node_name"]).strip(),
                    str(row["gpu_model"]).strip(),
                    _positive_int(row["gpu_capacity_num"], "gpu_capacity_num"),
                )
            )

    jobs: list[TraceJob] = []
    seen: set[str] = set()
    with job_info_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "job_name",
            "gpu_model",
            "gpu_request",
            "worker_num",
            "submit_time",
            "duration",
            "job_type",
        }
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("Alibaba job table has an unsupported header")
        for index, row in enumerate(reader):
            if gpu_models is not None and row["gpu_model"] not in gpu_models:
                continue
            if max_submit_time_seconds is not None and float(row["submit_time"]) > max_submit_time_seconds:
                continue
            job_id = str(row["job_name"]).strip()
            if not job_id or job_id in seen:
                raise ValueError(f"Alibaba job_name must be unique; invalid row {index}")
            seen.add(job_id)
            requested = float(row["gpu_request"]) * int(row["worker_num"])
            jobs.append(
                TraceJob(
                    job_id=job_id,
                    submit_time_seconds=_non_negative_float(row["submit_time"], "submit_time"),
                    duration_seconds=float(row["duration"]),
                    gpu_count=_positive_float(requested, "total GPU request"),
                    service_class=str(row["job_type"]).strip(),
                    gpu_model=str(row["gpu_model"]).strip(),
                )
            )
    source = {
        "dataset": "Alibaba cluster-trace-v2026-spot-gpu",
        "repository": "https://github.com/alibaba/clusterdata/tree/master/cluster-trace-v2026-spot-gpu",
        "commit": source_commit,
        "redistribution": "Raw and derived per-job data are not redistributed by SchedNav.",
        "node_info_sha256": _sha256_file(node_info_path),
        "job_info_sha256": _sha256_file(job_info_path),
        "filter": {
            "gpu_models": sorted(gpu_models) if gpu_models is not None else None,
            "max_submit_time_seconds": max_submit_time_seconds,
            "evaluation_start_seconds": evaluation_start_seconds,
            "evaluation_end_seconds": evaluation_end_seconds,
            "semantics": (
                "All selected arrivals through the inclusive evaluation end are replayed for state warm-up; only arrivals inside the explicit evaluation window contribute job SLO metrics, while allocation is integrated over that window."
                if evaluation_start_seconds is not None
                else "All selected arrivals from the source origin through the inclusive cutoff; jobs drain to completion."
            ),
        },
    }
    return write_canonical_trace(
        output_dir,
        trace_id=trace_id,
        time_origin=time_origin,
        source=source,
        nodes=nodes,
        jobs=jobs,
        evaluation_start_seconds=evaluation_start_seconds,
        evaluation_end_seconds=evaluation_end_seconds,
    )
