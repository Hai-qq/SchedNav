"""Authenticated, bounded HTTP task bridge for AgentTeams Workers."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path, PurePosixPath
import re
from threading import RLock
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from .contracts import canonical_sha256
from .multiwindow import aggregate_multiwindow_records
from .native_simulator import SimulationPolicy, build_metrics_report, simulate_trace
from .native_trace import load_canonical_trace
from .native_workload import analyze_canonical_workload
from .policy_portfolio import compare_policy_portfolio
from .policy_rank import rank_audited_policies
from .slo import audit_slo


CATALOG_SCHEMA = "schednav.host-bridge-config/v1"
TASK_SCHEMA = "schednav.bridge-task/v1"
REQUEST_SCHEMA = "schednav.bridge-request/v1"
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SAFE_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,127}$")
SAFE_TASK_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_BODY_BYTES = 64 * 1024
MCP_PROTOCOL_VERSION = "2025-03-26"
MAX_READ_ARTIFACT_BYTES = 512 * 1024
READABLE_ARTIFACT_SCHEMAS = {
    "schednav.metrics-report/v2",
    "schednav.workload-summary/v2",
    "schednav.policy-comparison/v1",
    "schednav.policy-portfolio/v1",
    "schednav.slo-audit/v1",
    "schednav.policy-ranking/v1",
    "schednav.trace/v1",
    "schednav.native-run/v1",
    "schednav.simulation-result/v1",
    "schednav.run-set-workloads/v1",
    "schednav.run-set-simulations/v1",
    "schednav.run-set-audit/v1",
    "schednav.multiwindow-summary/v1",
    "schednav.adaptive-study-design/v1",
    "schednav.controller-selections/v1",
    "schednav.adaptive-benchmark/v1",
}


class BridgeRequestError(ValueError):
    """A safe validation error that may be returned to an API caller."""


class IdempotencyConflict(BridgeRequestError):
    """An idempotency key was reused with different request content."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BridgeRequestError("Expected a JSON object")
    return value


def _require_exact_keys(value: dict[str, Any], required: set[str], optional: set[str] | None = None) -> None:
    optional = optional or set()
    missing = required - set(value)
    unexpected = set(value) - required - optional
    if missing or unexpected:
        raise BridgeRequestError(
            f"Invalid fields; missing={sorted(missing)}, unexpected={sorted(unexpected)}"
        )


def _require_safe_id(value: Any, field: str) -> str:
    normalized = str(value)
    if not SAFE_ID.fullmatch(normalized):
        raise BridgeRequestError(f"{field} is not a safe identifier")
    return normalized


def _is_relative_to(path: Path, root: Path) -> bool:
    path = path.resolve()
    root = root.resolve()
    try:
        path.relative_to(root)
        return True
    except ValueError:
        pass

    # Windows runners can expose the same temporary directory through a short
    # 8.3 alias and a long path. Compare existing ancestors by filesystem
    # identity so containment remains secure without relying on path spelling.
    if not root.exists():
        return False
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    while candidate.parent != candidate:
        try:
            if candidate.samefile(root):
                return True
        except OSError:
            return False
        candidate = candidate.parent
    return False


@dataclass(frozen=True)
class BridgeCatalog:
    project_root: Path
    artifact_root: Path
    task_root: Path
    run_configs: dict[str, Path]
    run_sets: dict[str, tuple[str, ...]]
    action_space: Path
    actions: dict[str, Path]
    slo_specs: dict[str, Path]
    baseline_metrics: dict[str, Path]
    max_workers: int

    @classmethod
    def load(cls, project_root: Path, config_path: Path) -> "BridgeCatalog":
        project_root = project_root.resolve()
        config_path = config_path.resolve()
        if not _is_relative_to(config_path, project_root) or not config_path.is_file():
            raise BridgeRequestError("Bridge config must be a file inside the project root")
        value = _load_json_object(config_path)
        _require_exact_keys(
            value,
            {
                "schema_version",
                "artifact_root",
                "task_subdir",
                "max_workers",
                "run_configs",
                "action_space",
                "actions",
                "slo_specs",
                "baseline_metrics",
            },
            {"run_sets"},
        )
        if value["schema_version"] != CATALOG_SCHEMA:
            raise BridgeRequestError(f"Expected schema_version={CATALOG_SCHEMA}")

        def project_path(raw: Any, *, must_exist: bool = True, directory: bool = False) -> Path:
            candidate = (project_root / str(raw)).resolve()
            if not _is_relative_to(candidate, project_root):
                raise BridgeRequestError("Catalog paths must stay inside the project root")
            if must_exist and not candidate.exists():
                raise BridgeRequestError("A catalog path does not exist")
            if must_exist and directory != candidate.is_dir():
                expected = "directory" if directory else "file"
                raise BridgeRequestError(f"A catalog path must be a {expected}")
            return candidate

        artifact_root = project_path(value["artifact_root"], must_exist=False)
        task_subdir = PurePosixPath(str(value["task_subdir"]))
        if task_subdir.is_absolute() or ".." in task_subdir.parts or not task_subdir.parts:
            raise BridgeRequestError("task_subdir must be a safe relative path")
        task_root = (artifact_root / Path(*task_subdir.parts)).resolve()
        if not _is_relative_to(task_root, artifact_root):
            raise BridgeRequestError("task_subdir escapes artifact_root")

        def catalog_map(raw: Any, field: str, *, allow_empty: bool = False) -> dict[str, Path]:
            if not isinstance(raw, dict) or (not raw and not allow_empty):
                requirement = "an object" if allow_empty else "a non-empty object"
                raise BridgeRequestError(f"{field} must be {requirement}")
            return {
                _require_safe_id(key, f"{field} key"): project_path(path)
                for key, path in raw.items()
            }

        max_workers = int(value["max_workers"])
        if max_workers != 1:
            raise BridgeRequestError("V1 host bridge requires max_workers=1")
        run_configs = catalog_map(value["run_configs"], "run_configs")
        raw_run_sets = value.get("run_sets", {})
        if not isinstance(raw_run_sets, dict):
            raise BridgeRequestError("run_sets must be an object")
        run_sets: dict[str, tuple[str, ...]] = {}
        for raw_id, raw_members in raw_run_sets.items():
            run_set_id = _require_safe_id(raw_id, "run_sets key")
            if not isinstance(raw_members, list) or not 1 <= len(raw_members) <= 12:
                raise BridgeRequestError("Each run set must contain 1-12 run config IDs")
            members = tuple(
                _require_safe_id(member, "run_set member") for member in raw_members
            )
            if len(set(members)) != len(members):
                raise BridgeRequestError("A run set cannot contain duplicate run configs")
            if any(member not in run_configs for member in members):
                raise BridgeRequestError("A run set references an unknown run config")
            run_sets[run_set_id] = members
        return cls(
            project_root=project_root,
            artifact_root=artifact_root,
            task_root=task_root,
            run_configs=run_configs,
            run_sets=run_sets,
            action_space=project_path(value["action_space"]),
            actions=catalog_map(value["actions"], "actions"),
            slo_specs=catalog_map(value["slo_specs"], "slo_specs"),
            baseline_metrics=catalog_map(
                value["baseline_metrics"], "baseline_metrics", allow_empty=True
            ),
            max_workers=max_workers,
        )

    def artifact_ref(self, path: Path) -> str:
        path = path.resolve()
        if not _is_relative_to(path, self.artifact_root):
            raise BridgeRequestError("Operation produced an artifact outside artifact_root")
        return path.relative_to(self.artifact_root).as_posix()

    def resolve_artifact_ref(self, raw: Any) -> Path:
        if not isinstance(raw, str) or not raw:
            raise BridgeRequestError("Artifact reference must be a non-empty string")
        reference = PurePosixPath(raw)
        if reference.is_absolute() or ".." in reference.parts or "\\" in raw:
            raise BridgeRequestError("Artifact reference must be a safe relative POSIX path")
        candidate = (self.artifact_root / Path(*reference.parts)).resolve()
        if not _is_relative_to(candidate, self.artifact_root) or not candidate.is_file():
            raise BridgeRequestError("Artifact reference does not identify an allowed file")
        return candidate

    @staticmethod
    def select(mapping: dict[str, Path], raw: Any, field: str) -> Path:
        identifier = _require_safe_id(raw, field)
        try:
            return mapping[identifier]
        except KeyError as exc:
            raise BridgeRequestError(f"Unknown {field}") from exc


OperationHandler = Callable[[dict[str, Any], Path, str], dict[str, Any]]


class BridgeService:
    """Persistent idempotent task service with a single bounded execution lane."""

    def __init__(
        self,
        catalog: BridgeCatalog,
        handlers: dict[str, OperationHandler] | None = None,
    ) -> None:
        self.catalog = catalog
        self.catalog.task_root.mkdir(parents=True, exist_ok=True)
        self._idempotency_root = self.catalog.task_root.parent / "idempotency"
        self._idempotency_root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=catalog.max_workers,
            thread_name_prefix="schednav-bridge",
        )
        self._handlers = handlers or {
            "analyze_workload": self._analyze_workload,
            "simulate_policy": self._simulate_policy,
            "compare_policies": self._compare_policies,
            "audit_slo": self._audit_slo,
            "rank_policies": self._rank_policies,
            "analyze_run_set": self._analyze_run_set,
            "simulate_run_set": self._simulate_run_set,
            "audit_run_set": self._audit_run_set,
        }

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)

    def supports_operation(self, operation: str) -> bool:
        """Return whether an operation is exposed by the bounded task service."""
        return operation in self._handlers

    def submit(self, request: dict[str, Any], idempotency_key: str) -> tuple[dict[str, Any], bool]:
        if not SAFE_IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            operation = request.get("operation") if isinstance(request, dict) else None
            raise BridgeRequestError(
                f"Idempotency-Key must be 8-128 safe characters for operation {operation!r}"
            )
        _require_exact_keys(request, {"schema_version", "operation", "arguments"})
        if request["schema_version"] != REQUEST_SCHEMA:
            raise BridgeRequestError(f"Expected schema_version={REQUEST_SCHEMA}")
        operation = _require_safe_id(request["operation"], "operation")
        if operation not in self._handlers:
            raise BridgeRequestError("Unsupported operation")
        arguments = request["arguments"]
        if not isinstance(arguments, dict):
            raise BridgeRequestError("arguments must be an object")
        normalized_arguments = self._validate_arguments(operation, arguments)
        normalized_request = {
            "schema_version": REQUEST_SCHEMA,
            "operation": operation,
            "arguments": normalized_arguments,
        }
        request_fingerprint = canonical_sha256(normalized_request)
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        receipt_path = self._idempotency_root / f"{key_hash}.json"

        with self._lock:
            if receipt_path.exists():
                receipt = _load_json_object(receipt_path)
                if receipt.get("request_fingerprint") != request_fingerprint:
                    raise IdempotencyConflict("Idempotency-Key was already used for another request")
                return self.get_task(str(receipt["task_id"])), False

            task_id = uuid.uuid4().hex
            task_dir = self.catalog.task_root / task_id
            task_dir.mkdir(parents=False, exist_ok=False)
            now = _utc_now()
            task = {
                "schema_version": TASK_SCHEMA,
                "task_id": task_id,
                "operation": operation,
                "status": "queued",
                "request_fingerprint": request_fingerprint,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "artifacts": {},
                "error": None,
            }
            _write_json_atomic(task_dir / "task.json", task)
            _write_json_atomic(
                task_dir / "request.json",
                {**normalized_request, "request_fingerprint": request_fingerprint},
            )
            _write_json_atomic(
                receipt_path,
                {
                    "schema_version": "schednav.bridge-idempotency/v1",
                    "key_sha256": key_hash,
                    "request_fingerprint": request_fingerprint,
                    "task_id": task_id,
                },
            )
            self._executor.submit(
                self._execute,
                task_id,
                operation,
                normalized_arguments,
            )
            return task, True

    def get_task(self, task_id: str) -> dict[str, Any]:
        if not SAFE_TASK_ID.fullmatch(task_id):
            raise BridgeRequestError("Invalid task id")
        task_path = self.catalog.task_root / task_id / "task.json"
        if not task_path.is_file():
            raise FileNotFoundError(task_id)
        return _load_json_object(task_path)

    def _validate_arguments(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation == "analyze_workload":
            _require_exact_keys(arguments, {"run_config_id"}, {"sample_interval_seconds"})
            run_config_id = _require_safe_id(arguments["run_config_id"], "run_config_id")
            self.catalog.select(self.catalog.run_configs, run_config_id, "run_config_id")
            sample = int(arguments.get("sample_interval_seconds", 3600))
            if sample < 60 or sample > 86400:
                raise BridgeRequestError("sample_interval_seconds must be between 60 and 86400")
            return {"run_config_id": run_config_id, "sample_interval_seconds": sample}
        if operation == "simulate_policy":
            _require_exact_keys(arguments, {"run_config_id", "action_id"})
            run_config_id = _require_safe_id(arguments["run_config_id"], "run_config_id")
            action_id = _require_safe_id(arguments["action_id"], "action_id")
            self.catalog.select(self.catalog.run_configs, run_config_id, "run_config_id")
            self.catalog.select(self.catalog.actions, action_id, "action_id")
            return {"run_config_id": run_config_id, "action_id": action_id}
        if operation == "compare_policies":
            _require_exact_keys(arguments, {"metrics_refs"})
            refs = self._validate_ref_list(arguments["metrics_refs"], "metrics_refs")
            if not 3 <= len(refs) <= 5:
                raise BridgeRequestError("compare_policies requires 3-5 metrics_refs")
            return {"metrics_refs": refs}
        if operation == "audit_slo":
            _require_exact_keys(
                arguments,
                {"metrics_ref", "slo_spec_id"},
                {"baseline_metrics_id", "baseline_metrics_ref"},
            )
            baseline_keys = {
                key for key in ("baseline_metrics_id", "baseline_metrics_ref") if key in arguments
            }
            if len(baseline_keys) != 1:
                raise BridgeRequestError(
                    "audit_slo requires exactly one baseline_metrics_id or baseline_metrics_ref"
                )
            metrics_ref = self._validate_ref(arguments["metrics_ref"], "metrics_ref")
            slo_spec_id = _require_safe_id(arguments["slo_spec_id"], "slo_spec_id")
            self.catalog.select(self.catalog.slo_specs, slo_spec_id, "slo_spec_id")
            normalized = {
                "metrics_ref": metrics_ref,
                "slo_spec_id": slo_spec_id,
            }
            if "baseline_metrics_id" in arguments:
                baseline_id = _require_safe_id(
                    arguments["baseline_metrics_id"], "baseline_metrics_id"
                )
                self.catalog.select(
                    self.catalog.baseline_metrics, baseline_id, "baseline_metrics_id"
                )
                normalized["baseline_metrics_id"] = baseline_id
            else:
                normalized["baseline_metrics_ref"] = self._validate_ref(
                    arguments["baseline_metrics_ref"], "baseline_metrics_ref"
                )
            return normalized
        if operation == "rank_policies":
            _require_exact_keys(arguments, {"metrics_refs", "audit_refs", "slo_spec_id"})
            metrics_refs = self._validate_ref_list(arguments["metrics_refs"], "metrics_refs")
            audit_refs = self._validate_ref_list(arguments["audit_refs"], "audit_refs")
            if not 3 <= len(metrics_refs) <= 5 or len(metrics_refs) != len(audit_refs):
                raise BridgeRequestError("rank_policies requires matching 3-5 metrics and audit refs")
            slo_spec_id = _require_safe_id(arguments["slo_spec_id"], "slo_spec_id")
            self.catalog.select(self.catalog.slo_specs, slo_spec_id, "slo_spec_id")
            return {
                "metrics_refs": metrics_refs,
                "audit_refs": audit_refs,
                "slo_spec_id": slo_spec_id,
            }
        if operation == "analyze_run_set":
            _require_exact_keys(arguments, {"run_set_id"}, {"sample_interval_seconds"})
            run_set_id = _require_safe_id(arguments["run_set_id"], "run_set_id")
            if run_set_id not in self.catalog.run_sets:
                raise BridgeRequestError("Unknown run_set_id")
            sample = int(arguments.get("sample_interval_seconds", 3600))
            if sample < 60 or sample > 86400:
                raise BridgeRequestError("sample_interval_seconds must be between 60 and 86400")
            return {"run_set_id": run_set_id, "sample_interval_seconds": sample}
        if operation == "simulate_run_set":
            _require_exact_keys(arguments, {"run_set_id", "action_ids"}, {"repetitions"})
            run_set_id = _require_safe_id(arguments["run_set_id"], "run_set_id")
            if run_set_id not in self.catalog.run_sets:
                raise BridgeRequestError("Unknown run_set_id")
            raw_actions = arguments["action_ids"]
            if not isinstance(raw_actions, list) or not 3 <= len(raw_actions) <= 5:
                raise BridgeRequestError("simulate_run_set requires 3-5 action_ids")
            action_ids = [
                _require_safe_id(action_id, "action_id") for action_id in raw_actions
            ]
            if len(set(action_ids)) != len(action_ids):
                raise BridgeRequestError("action_ids cannot contain duplicates")
            for action_id in action_ids:
                self.catalog.select(self.catalog.actions, action_id, "action_id")
            repetitions = int(arguments.get("repetitions", 2))
            if repetitions not in {1, 2}:
                raise BridgeRequestError("repetitions must be 1 or 2")
            return {
                "run_set_id": run_set_id,
                "action_ids": action_ids,
                "repetitions": repetitions,
            }
        if operation == "audit_run_set":
            _require_exact_keys(
                arguments,
                {"simulations_ref", "slo_spec_id", "baseline_action_id"},
            )
            simulations_ref = self._validate_ref(
                arguments["simulations_ref"], "simulations_ref"
            )
            slo_spec_id = _require_safe_id(arguments["slo_spec_id"], "slo_spec_id")
            self.catalog.select(self.catalog.slo_specs, slo_spec_id, "slo_spec_id")
            baseline_action_id = _require_safe_id(
                arguments["baseline_action_id"], "baseline_action_id"
            )
            self.catalog.select(self.catalog.actions, baseline_action_id, "baseline_action_id")
            return {
                "simulations_ref": simulations_ref,
                "slo_spec_id": slo_spec_id,
                "baseline_action_id": baseline_action_id,
            }
        raise BridgeRequestError("Unsupported operation")

    def _validate_ref(self, raw: Any, field: str) -> str:
        if not isinstance(raw, str):
            raise BridgeRequestError(f"{field} must be a string")
        self.catalog.resolve_artifact_ref(raw)
        return PurePosixPath(raw).as_posix()

    def _validate_ref_list(self, raw: Any, field: str) -> list[str]:
        if not isinstance(raw, list) or not raw:
            raise BridgeRequestError(f"{field} must be a non-empty list")
        refs = [self._validate_ref(item, field) for item in raw]
        if len(set(refs)) != len(refs):
            raise BridgeRequestError(f"{field} cannot contain duplicates")
        return refs

    def _execute(
        self,
        task_id: str,
        operation: str,
        arguments: dict[str, Any],
    ) -> None:
        task_dir = self.catalog.task_root / task_id
        task_path = task_dir / "task.json"
        task = _load_json_object(task_path)
        task["status"] = "running"
        task["started_at"] = _utc_now()
        _write_json_atomic(task_path, task)
        try:
            artifacts = self._handlers[operation](arguments, task_dir, task_id)
            task["artifacts"] = artifacts
            task["status"] = "succeeded"
        except Exception as exc:  # Task boundary deliberately converts failures to evidence.
            failure = {
                "schema_version": "schednav.bridge-failure/v1",
                "error_type": type(exc).__name__,
                "message": self._sanitize_local_error(str(exc)),
            }
            _write_json_atomic(task_dir / "failure.local.json", failure)
            task["status"] = "failed"
            task["error"] = {
                "code": "operation_failed",
                "message": "Operation failed; inspect the machine-local failure artifact.",
            }
        task["finished_at"] = _utc_now()
        _write_json_atomic(task_path, task)

    @staticmethod
    def _sanitize_local_error(message: str) -> str:
        sanitized = re.sub(
            r"(?i)(api[_-]?key|password|token|secret)(\s*[:=]\s*)([^\s,;]+)",
            r"\1\2[REDACTED]",
            message,
        )
        sanitized = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", sanitized)
        return sanitized[:2000]

    def _write_result(self, task_dir: Path, name: str, value: dict[str, Any]) -> str:
        path = task_dir / name
        _write_json_atomic(path, value)
        return self.catalog.artifact_ref(path)

    def _trace_from_config(self, config_path: Path) -> tuple[Path, Any]:
        value = _load_json_object(config_path)
        if value.get("schema_version") != "schednav.native-run-config/v1":
            raise BridgeRequestError(
                "Run configs must use schema_version=schednav.native-run-config/v1"
            )
        _require_exact_keys(value, {"schema_version", "trace_manifest"})
        relative = Path(str(value["trace_manifest"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise BridgeRequestError("trace_manifest must be a safe project-relative path")
        trace_path = (self.catalog.project_root / relative).resolve()
        if not _is_relative_to(trace_path, self.catalog.project_root) or not trace_path.is_file():
            raise BridgeRequestError("trace_manifest does not identify a canonical trace")
        return trace_path, load_canonical_trace(trace_path)

    def _analyze_workload(
        self,
        arguments: dict[str, Any],
        task_dir: Path,
        _task_id: str,
    ) -> dict[str, Any]:
        config_path = self.catalog.select(
            self.catalog.run_configs, arguments["run_config_id"], "run_config_id"
        )
        _trace_path, trace = self._trace_from_config(config_path)
        result = analyze_canonical_workload(
            trace,
            sample_interval_seconds=arguments["sample_interval_seconds"],
        )
        return {"workload_summary": self._write_result(task_dir, "workload-summary.json", result)}

    def _simulate_policy(
        self,
        arguments: dict[str, Any],
        task_dir: Path,
        _task_id: str,
    ) -> dict[str, Any]:
        base_config = self.catalog.select(
            self.catalog.run_configs, arguments["run_config_id"], "run_config_id"
        )
        action = self.catalog.select(self.catalog.actions, arguments["action_id"], "action_id")
        trace_path, trace = self._trace_from_config(base_config)
        action_value = _load_json_object(action)
        if action_value.get("schema_version") != "schednav.simulation-policy/v1":
            raise BridgeRequestError(
                "Policies must use schema_version=schednav.simulation-policy/v1"
            )
        policy = SimulationPolicy.from_dict(action_value)
        simulation_result = simulate_trace(trace, policy)
        metrics = build_metrics_report(simulation_result)
        run_spec = {
            "schema_version": "schednav.native-run/v1",
            "trace_manifest": trace_path.relative_to(self.catalog.project_root).as_posix(),
            "trace_fingerprint": trace.fingerprint,
            "policy": action_value,
            "policy_fingerprint": policy.fingerprint,
        }
        run_spec["run_fingerprint"] = canonical_sha256(run_spec)
        run_spec_path = task_dir / "run-spec.json"
        result_path = task_dir / "simulation-result.json"
        metrics_path = task_dir / "metrics.json"
        _write_json_atomic(run_spec_path, run_spec)
        _write_json_atomic(result_path, simulation_result)
        _write_json_atomic(metrics_path, metrics)
        return {
            "run_spec": self.catalog.artifact_ref(run_spec_path),
            "simulation_result": self.catalog.artifact_ref(result_path),
            "metrics": self.catalog.artifact_ref(metrics_path),
        }

    def _compare_policies(
        self,
        arguments: dict[str, Any],
        task_dir: Path,
        _task_id: str,
    ) -> dict[str, Any]:
        metrics_paths = [
            self.catalog.resolve_artifact_ref(reference)
            for reference in arguments["metrics_refs"]
        ]
        result = compare_policy_portfolio(metrics_paths)
        return {"portfolio": self._write_result(task_dir, "policy-portfolio.json", result)}

    def _audit_slo(
        self,
        arguments: dict[str, Any],
        task_dir: Path,
        _task_id: str,
    ) -> dict[str, Any]:
        metrics_path = self.catalog.resolve_artifact_ref(arguments["metrics_ref"])
        slo_path = self.catalog.select(
            self.catalog.slo_specs, arguments["slo_spec_id"], "slo_spec_id"
        )
        baseline_path = (
            self.catalog.select(
                self.catalog.baseline_metrics,
                arguments["baseline_metrics_id"],
                "baseline_metrics_id",
            )
            if "baseline_metrics_id" in arguments
            else self.catalog.resolve_artifact_ref(arguments["baseline_metrics_ref"])
        )
        result = audit_slo(metrics_path, slo_path, baseline_path)
        return {"slo_audit": self._write_result(task_dir, "slo-audit.json", result)}

    def _rank_policies(
        self,
        arguments: dict[str, Any],
        task_dir: Path,
        _task_id: str,
    ) -> dict[str, Any]:
        metrics_paths = [
            self.catalog.resolve_artifact_ref(reference)
            for reference in arguments["metrics_refs"]
        ]
        audit_paths = [
            self.catalog.resolve_artifact_ref(reference)
            for reference in arguments["audit_refs"]
        ]
        slo_path = self.catalog.select(
            self.catalog.slo_specs, arguments["slo_spec_id"], "slo_spec_id"
        )
        result = rank_audited_policies(metrics_paths, audit_paths, slo_path)
        return {"ranking": self._write_result(task_dir, "policy-ranking.json", result)}

    def _run_set_traces(self, run_set_id: str) -> list[tuple[str, Path, Any]]:
        traces: list[tuple[str, Path, Any]] = []
        for run_config_id in self.catalog.run_sets[run_set_id]:
            config_path = self.catalog.run_configs[run_config_id]
            trace_path, trace = self._trace_from_config(config_path)
            traces.append((run_config_id, trace_path, trace))
        return traces

    @staticmethod
    def _run_set_selection_fingerprint(
        run_set_id: str, traces: list[tuple[str, Path, Any]]
    ) -> str:
        return canonical_sha256(
            {
                "run_set_id": run_set_id,
                "windows": [
                    {
                        "run_config_id": run_config_id,
                        "trace_fingerprint": trace.fingerprint,
                    }
                    for run_config_id, _trace_path, trace in traces
                ],
            }
        )

    def _analyze_run_set(
        self,
        arguments: dict[str, Any],
        task_dir: Path,
        _task_id: str,
    ) -> dict[str, Any]:
        run_set_id = arguments["run_set_id"]
        traces = self._run_set_traces(run_set_id)
        windows: list[dict[str, Any]] = []
        for run_config_id, _trace_path, trace in traces:
            workload = analyze_canonical_workload(
                trace,
                sample_interval_seconds=arguments["sample_interval_seconds"],
            )
            ref = self._write_result(
                task_dir,
                f"workload-{run_config_id}.json",
                workload,
            )
            windows.append(
                {
                    "run_config_id": run_config_id,
                    "trace_id": trace.trace_id,
                    "trace_fingerprint": trace.fingerprint,
                    "workload_ref": ref,
                    "workload_fingerprint": workload["workload_fingerprint"],
                    "population": {
                        service_class: workload["population"][service_class]["job_count"]
                        for service_class in ("HP", "Spot")
                    },
                    "regime_signals": workload["regime_signals"],
                }
            )
        report: dict[str, Any] = {
            "schema_version": "schednav.run-set-workloads/v1",
            "run_set_id": run_set_id,
            "selection_fingerprint": self._run_set_selection_fingerprint(
                run_set_id, traces
            ),
            "sample_interval_seconds": arguments["sample_interval_seconds"],
            "window_count": len(windows),
            "windows": windows,
        }
        report["workload_set_fingerprint"] = canonical_sha256(report)
        return {
            "run_set_workloads": self._write_result(
                task_dir, "run-set-workloads.json", report
            )
        }

    def _simulate_run_set(
        self,
        arguments: dict[str, Any],
        task_dir: Path,
        _task_id: str,
    ) -> dict[str, Any]:
        run_set_id = arguments["run_set_id"]
        traces = self._run_set_traces(run_set_id)
        policies: list[tuple[str, SimulationPolicy, dict[str, Any]]] = []
        for action_id in arguments["action_ids"]:
            path = self.catalog.actions[action_id]
            value = _load_json_object(path)
            policy = SimulationPolicy.from_dict(value)
            if policy.action_id != action_id:
                raise BridgeRequestError("Catalog action ID does not match policy action_id")
            policies.append((action_id, policy, value))

        windows: list[dict[str, Any]] = []
        for run_config_id, trace_path, trace in traces:
            actions: dict[str, Any] = {}
            for action_id, policy, policy_value in policies:
                result_fingerprints: list[str] = []
                metrics_fingerprints: list[str] = []
                primary_result: dict[str, Any] | None = None
                primary_metrics: dict[str, Any] | None = None
                for _repetition in range(arguments["repetitions"]):
                    simulation_result = simulate_trace(trace, policy)
                    metrics = build_metrics_report(simulation_result)
                    result_fingerprints.append(simulation_result["result_fingerprint"])
                    metrics_fingerprints.append(metrics["metrics_fingerprint"])
                    if primary_result is None:
                        primary_result = simulation_result
                        primary_metrics = metrics
                deterministic = (
                    len(set(result_fingerprints)) == 1
                    and len(set(metrics_fingerprints)) == 1
                )
                if not deterministic or primary_result is None or primary_metrics is None:
                    raise RuntimeError(
                        f"Determinism check failed for {run_config_id}/{action_id}"
                    )
                result_path = task_dir / f"result-{run_config_id}-{action_id}.json"
                metrics_path = task_dir / f"metrics-{run_config_id}-{action_id}.json"
                _write_json_atomic(result_path, primary_result)
                _write_json_atomic(metrics_path, primary_metrics)
                actions[action_id] = {
                    "policy": policy_value,
                    "policy_fingerprint": policy.fingerprint,
                    "simulation_result_ref": self.catalog.artifact_ref(result_path),
                    "metrics_ref": self.catalog.artifact_ref(metrics_path),
                    "result_fingerprints": result_fingerprints,
                    "metrics_fingerprints": metrics_fingerprints,
                    "deterministic_repetitions": deterministic,
                }
            windows.append(
                {
                    "run_config_id": run_config_id,
                    "trace_manifest": trace_path.relative_to(
                        self.catalog.project_root
                    ).as_posix(),
                    "trace_id": trace.trace_id,
                    "trace_fingerprint": trace.fingerprint,
                    "actions": actions,
                }
            )
        index: dict[str, Any] = {
            "schema_version": "schednav.run-set-simulations/v1",
            "run_set_id": run_set_id,
            "selection_fingerprint": self._run_set_selection_fingerprint(
                run_set_id, traces
            ),
            "action_ids": arguments["action_ids"],
            "repetitions": arguments["repetitions"],
            "window_count": len(windows),
            "simulation_count": (
                len(windows) * len(policies) * arguments["repetitions"]
            ),
            "windows": windows,
        }
        index["run_set_simulations_fingerprint"] = canonical_sha256(index)
        return {
            "run_set_simulations": self._write_result(
                task_dir, "run-set-simulations.json", index
            )
        }

    def _audit_run_set(
        self,
        arguments: dict[str, Any],
        task_dir: Path,
        _task_id: str,
    ) -> dict[str, Any]:
        simulations_path = self.catalog.resolve_artifact_ref(
            arguments["simulations_ref"]
        )
        index = _load_json_object(simulations_path)
        if index.get("schema_version") != "schednav.run-set-simulations/v1":
            raise BridgeRequestError("simulations_ref must use run-set simulations schema")
        supplied = index.get("run_set_simulations_fingerprint")
        if canonical_sha256(
            {
                key: value
                for key, value in index.items()
                if key != "run_set_simulations_fingerprint"
            }
        ) != supplied:
            raise BridgeRequestError("simulations_ref has an invalid fingerprint")
        baseline_action_id = arguments["baseline_action_id"]
        if baseline_action_id not in index["action_ids"]:
            raise BridgeRequestError("The baseline action is missing from simulations_ref")
        slo_path = self.catalog.slo_specs[arguments["slo_spec_id"]]
        records: list[dict[str, Any]] = []
        windows_index: list[dict[str, Any]] = []

        for window in index["windows"]:
            run_config_id = _require_safe_id(
                window["run_config_id"], "run_config_id"
            )
            trace_path, trace = self._trace_from_config(
                self.catalog.run_configs[run_config_id]
            )
            workload = analyze_canonical_workload(trace, sample_interval_seconds=3600)
            metrics_paths = {
                action_id: self.catalog.resolve_artifact_ref(
                    window["actions"][action_id]["metrics_ref"]
                )
                for action_id in index["action_ids"]
            }
            baseline_path = metrics_paths[baseline_action_id]
            portfolio = compare_policy_portfolio(list(metrics_paths.values()))
            portfolio_ref = self._write_result(
                task_dir,
                f"portfolio-{run_config_id}.json",
                portfolio,
            )
            audits: dict[str, dict[str, Any]] = {}
            audit_paths: dict[str, Path] = {}
            audit_refs: dict[str, str] = {}
            for action_id in index["action_ids"]:
                audit = audit_slo(metrics_paths[action_id], slo_path, baseline_path)
                audit_path = task_dir / f"audit-{run_config_id}-{action_id}.json"
                _write_json_atomic(audit_path, audit)
                audits[action_id] = audit
                audit_paths[action_id] = audit_path
                audit_refs[action_id] = self.catalog.artifact_ref(audit_path)
            ranking = rank_audited_policies(
                list(metrics_paths.values()),
                [audit_paths[action_id] for action_id in index["action_ids"]],
                slo_path,
            )
            ranking_ref = self._write_result(
                task_dir,
                f"ranking-{run_config_id}.json",
                ranking,
            )
            metrics_values = {
                action_id: _load_json_object(metrics_paths[action_id])
                for action_id in index["action_ids"]
            }
            action_by_fingerprint = {
                metrics["policy_fingerprint"]: action_id
                for action_id, metrics in metrics_values.items()
            }
            policy_records: list[dict[str, Any]] = []
            for action_id in index["action_ids"]:
                metrics = metrics_values[action_id]
                audit = audits[action_id]
                soft = next(
                    (
                        bool(item["passed"])
                        for item in audit["results"]
                        if item["id"] == "allocation-soft-target"
                    ),
                    None,
                )
                action_evidence = window["actions"][action_id]
                policy_records.append(
                    {
                        "action_id": action_id,
                        "policy_fingerprint": metrics["policy_fingerprint"],
                        "result_fingerprint": action_evidence["result_fingerprints"][0],
                        "metrics_fingerprint": metrics["metrics_fingerprint"],
                        "deterministic_repetitions": action_evidence[
                            "deterministic_repetitions"
                        ],
                        "allocation_rate_mean": metrics["cluster"][
                            "allocation_rate_mean"
                        ],
                        "hp_completion_rate": metrics["jobs"]["HP"][
                            "completion_rate"
                        ],
                        "hp_preempted_job_count": metrics["jobs"]["HP"][
                            "preempted_job_count"
                        ],
                        "hp_jct_p95_seconds": metrics["jobs"]["HP"]["jct_seconds"][
                            "p95"
                        ],
                        "hp_queue_p95_seconds": metrics["jobs"]["HP"][
                            "queue_seconds"
                        ]["p95"],
                        "spot_completion_rate": metrics["jobs"]["Spot"][
                            "completion_rate"
                        ],
                        "spot_jct_p95_seconds": metrics["jobs"]["Spot"][
                            "jct_seconds"
                        ]["p95"],
                        "spot_eviction_rate_per_run": metrics["preemption_events"][
                            "eviction_rate_per_run"
                        ],
                        "spot_guarantee_success_rate": metrics["spot_guarantee"][
                            "success_rate"
                        ],
                        "audit_fingerprint": audit["audit_fingerprint"],
                        "hard_slo_passed": audit["audit_passed"],
                        "allocation_soft_target_met": soft,
                    }
                )
            record: dict[str, Any] = {
                "window_id": run_config_id,
                "date": run_config_id,
                "stratum": {"source": "cataloged_run_set"},
                "window_seconds": {
                    "start": metrics_values[baseline_action_id]["window_seconds"][
                        "evaluation_start"
                    ],
                    "end": metrics_values[baseline_action_id]["window_seconds"][
                        "evaluation_end"
                    ],
                },
                "trace_fingerprint": trace.fingerprint,
                "workload_fingerprint": workload["workload_fingerprint"],
                "population": {
                    service_class: workload["population"][service_class]["job_count"]
                    for service_class in ("HP", "Spot")
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
            records.append(record)
            windows_index.append(
                {
                    "run_config_id": run_config_id,
                    "trace_manifest": trace_path.relative_to(
                        self.catalog.project_root
                    ).as_posix(),
                    "portfolio_ref": portfolio_ref,
                    "audit_refs": audit_refs,
                    "ranking_ref": ranking_ref,
                    "window_record_fingerprint": record[
                        "window_record_fingerprint"
                    ],
                }
            )
        summary = aggregate_multiwindow_records(
            records,
            selection_fingerprint=index["selection_fingerprint"],
            baseline_action_id=baseline_action_id,
        )
        summary_ref = self._write_result(
            task_dir, "multiwindow-summary.json", summary
        )
        audit_index: dict[str, Any] = {
            "schema_version": "schednav.run-set-audit/v1",
            "run_set_id": index["run_set_id"],
            "simulations_ref": arguments["simulations_ref"],
            "run_set_simulations_fingerprint": supplied,
            "slo_spec_id": arguments["slo_spec_id"],
            "baseline_action_id": baseline_action_id,
            "window_count": len(records),
            "windows": windows_index,
            "multiwindow_summary_ref": summary_ref,
            "multiwindow_fingerprint": summary["multiwindow_fingerprint"],
        }
        audit_index["run_set_audit_fingerprint"] = canonical_sha256(audit_index)
        return {
            "run_set_audit": self._write_result(
                task_dir, "run-set-audit.json", audit_index
            ),
            "multiwindow_summary": summary_ref,
        }


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        service: BridgeService,
        tokens: tuple[str, ...],
        auth_gateway_url: str | None = None,
    ) -> None:
        super().__init__(address, BridgeRequestHandler)
        self.service = service
        self.tokens = tokens
        self.auth_gateway_url = auth_gateway_url
        self._auth_cache: dict[str, float] = {}
        self._auth_lock = RLock()

    def validate_token(self, token: str) -> bool:
        if len(token) < 16:
            return False
        if any(hmac.compare_digest(token, allowed) for allowed in self.tokens):
            return True
        if not self.auth_gateway_url:
            return False
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).timestamp()
        with self._auth_lock:
            if self._auth_cache.get(digest, 0.0) > now:
                return True
        request = Request(
            self.auth_gateway_url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "AgentTeams/SchedNav",
            },
            method="GET",
        )
        try:
            # Authentication is a loopback control-plane call. Bypass ambient
            # HTTP proxies so a desktop proxy cannot replace the gateway status.
            with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                valid = 200 <= response.status < 400
        except HTTPError as exc:
            # Protected gateways may authenticate before returning a route or
            # method response (for example GET chat/completions -> 404).
            valid = exc.code not in {
                HTTPStatus.UNAUTHORIZED.value,
                HTTPStatus.FORBIDDEN.value,
            }
        except (URLError, TimeoutError):
            valid = False
        if valid:
            with self._auth_lock:
                self._auth_cache[digest] = now + 60.0
        return valid


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server: BridgeHTTPServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        prefix = "Bearer "
        return supplied.startswith(prefix) and self.server.validate_token(supplied[len(prefix):])

    def _send(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._send(status, {"error": {"code": code, "message": message}})

    def _send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status.value)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    @staticmethod
    def _mcp_tools() -> list[dict[str, Any]]:
        common = {
            "idempotency_key": {
                "type": "string",
                "minLength": 8,
                "maxLength": 128,
                "description": "Stable key reused only for the same logical request.",
            }
        }
        return [
            {
                "name": "analyze_workload",
                "description": "Create a fingerprinted workload summary for one cataloged trace window.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["idempotency_key", "run_config_id"],
                    "properties": {
                        **common,
                        "run_config_id": {"type": "string"},
                        "sample_interval_seconds": {
                            "type": "integer",
                            "minimum": 60,
                            "maximum": 86400,
                        },
                    },
                },
            },
            {
                "name": "simulate_policy",
                "description": "Materialize and execute one cataloged policy in the deterministic simulator.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["idempotency_key", "run_config_id", "action_id"],
                    "properties": {
                        **common,
                        "run_config_id": {"type": "string"},
                        "action_id": {"type": "string"},
                    },
                },
            },
            {
                "name": "compare_policies",
                "description": "Compare three to five canonical metrics artifacts without selecting a winner.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["idempotency_key", "metrics_refs"],
                    "properties": {
                        **common,
                        "metrics_refs": {
                            "type": "array",
                            "minItems": 3,
                            "maxItems": 5,
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            {
                "name": "audit_slo",
                "description": "Audit canonical metrics against a cataloged SLO and FIFO baseline.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "idempotency_key",
                        "metrics_ref",
                        "slo_spec_id"
                    ],
                    "oneOf": [
                        {"required": ["baseline_metrics_id"]},
                        {"required": ["baseline_metrics_ref"]}
                    ],
                    "properties": {
                        **common,
                        "metrics_ref": {"type": "string"},
                        "slo_spec_id": {"type": "string"},
                        "baseline_metrics_id": {"type": "string"},
                        "baseline_metrics_ref": {"type": "string"},
                    },
                },
            },
            {
                "name": "rank_policies",
                "description": "Apply the SLO-declared hierarchy to matching metrics and audit artifacts.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "idempotency_key",
                        "metrics_refs",
                        "audit_refs",
                        "slo_spec_id",
                    ],
                    "properties": {
                        **common,
                        "metrics_refs": {
                            "type": "array",
                            "minItems": 3,
                            "maxItems": 5,
                            "items": {"type": "string"},
                        },
                        "audit_refs": {
                            "type": "array",
                            "minItems": 3,
                            "maxItems": 5,
                            "items": {"type": "string"},
                        },
                        "slo_spec_id": {"type": "string"},
                    },
                },
            },
            {
                "name": "analyze_run_set",
                "description": "Analyze every cataloged window in one bounded run set.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["idempotency_key", "run_set_id"],
                    "properties": {
                        **common,
                        "run_set_id": {"type": "string"},
                        "sample_interval_seconds": {
                            "type": "integer",
                            "minimum": 60,
                            "maximum": 86400,
                        },
                    },
                },
            },
            {
                "name": "simulate_run_set",
                "description": "Run three to five cataloged policies across every window in a bounded run set.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["idempotency_key", "run_set_id", "action_ids"],
                    "properties": {
                        **common,
                        "run_set_id": {"type": "string"},
                        "action_ids": {
                            "type": "array",
                            "minItems": 3,
                            "maxItems": 5,
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                        "repetitions": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 2,
                        },
                    },
                },
            },
            {
                "name": "audit_run_set",
                "description": "Audit and hierarchically rank a complete run-set simulation index window by window.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "idempotency_key",
                        "simulations_ref",
                        "slo_spec_id",
                        "baseline_action_id",
                    ],
                    "properties": {
                        **common,
                        "simulations_ref": {"type": "string"},
                        "slo_spec_id": {"type": "string"},
                        "baseline_action_id": {"type": "string"},
                    },
                },
            },
            {
                "name": "get_task",
                "description": "Read one bridge task by its opaque task id.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task_id"],
                    "properties": {
                        "task_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}
                    },
                },
            },
            {
                "name": "read_artifact",
                "description": "Read one small schema-approved JSON artifact by its relative reference.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["artifact_ref"],
                    "properties": {"artifact_ref": {"type": "string"}},
                },
            },
        ]

    @staticmethod
    def _mcp_result(value: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(value, ensure_ascii=False, sort_keys=True),
                }
            ],
            "structuredContent": value,
            "isError": is_error,
        }

    def _handle_mcp(self, request: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any] | None]:
        if request.get("jsonrpc") != "2.0" or "method" not in request:
            return HTTPStatus.BAD_REQUEST, {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32600, "message": "Invalid JSON-RPC request"},
            }
        request_id = request.get("id")
        method = request["method"]
        if method == "notifications/initialized":
            return HTTPStatus.ACCEPTED, None
        if request_id is None:
            return HTTPStatus.ACCEPTED, None
        if method == "initialize":
            result = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "schednav-host-bridge", "version": "0.1.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self._mcp_tools()}
        elif method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                result = self._mcp_result(
                    {"error": {"code": "invalid_request", "message": "Tool name is required"}},
                    True,
                )
            else:
                name = params["name"]
                arguments = params.get("arguments", {})
                try:
                    if not isinstance(arguments, dict):
                        raise BridgeRequestError("Tool arguments must be an object")
                    if name == "get_task":
                        _require_exact_keys(arguments, {"task_id"})
                        result = self._mcp_result(
                            self.server.service.get_task(str(arguments["task_id"]))
                        )
                    elif name == "read_artifact":
                        _require_exact_keys(arguments, {"artifact_ref"})
                        path = self.server.service.catalog.resolve_artifact_ref(
                            arguments["artifact_ref"]
                        )
                        if path.stat().st_size > MAX_READ_ARTIFACT_BYTES:
                            raise BridgeRequestError("Artifact exceeds the structured read limit")
                        document = _load_json_object(path)
                        schema_version = document.get("schema_version")
                        if schema_version not in READABLE_ARTIFACT_SCHEMAS:
                            raise BridgeRequestError("Artifact schema is not readable through MCP")
                        result = self._mcp_result(
                            {
                                "artifact_ref": self.server.service.catalog.artifact_ref(path),
                                "schema_version": schema_version,
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                "document": document,
                            }
                        )
                    else:
                        if not self.server.service.supports_operation(name):
                            raise BridgeRequestError(f"Unsupported MCP tool name: {name!r}")
                        idempotency_key = str(arguments.get("idempotency_key", ""))
                        task_arguments = {
                            key: value for key, value in arguments.items() if key != "idempotency_key"
                        }
                        task, _created = self.server.service.submit(
                            {
                                "schema_version": REQUEST_SCHEMA,
                                "operation": name,
                                "arguments": task_arguments,
                            },
                            idempotency_key,
                        )
                        result = self._mcp_result(task)
                except FileNotFoundError:
                    result = self._mcp_result(
                        {"error": {"code": "not_found", "message": "Task not found"}},
                        True,
                    )
                except IdempotencyConflict as exc:
                    result = self._mcp_result(
                        {"error": {"code": "idempotency_conflict", "message": str(exc)}},
                        True,
                    )
                except BridgeRequestError as exc:
                    result = self._mcp_result(
                        {"error": {"code": "invalid_request", "message": str(exc)}},
                        True,
                    )
        else:
            return HTTPStatus.OK, {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        return HTTPStatus.OK, {"jsonrpc": "2.0", "id": request_id, "result": result}

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send(HTTPStatus.OK, {"status": "ok", "service": "schednav-host-bridge"})
            return
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "Bearer token required")
            return
        match = re.fullmatch(r"/v1/tasks/([0-9a-f]{32})", path)
        if not match:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "Route not found")
            return
        try:
            self._send(HTTPStatus.OK, self.server.service.get_task(match.group(1)))
        except FileNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "Task not found")
        except BridgeRequestError as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {"/v1/tasks", "/mcp"}:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "Route not found")
            return
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "Bearer token required")
            return
        if self.headers.get_content_type() != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "invalid_content_type", "Use application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "invalid_body_size", "Invalid request size")
            return
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(request, dict):
                raise BridgeRequestError("Request body must be an object")
            if path == "/mcp":
                status, response = self._handle_mcp(request)
                if response is None:
                    self._send_empty(status)
                else:
                    self._send(status, response)
                return
            task, created = self.server.service.submit(
                request,
                self.headers.get("Idempotency-Key", ""),
            )
            self._send(HTTPStatus.ACCEPTED if created else HTTPStatus.OK, task)
        except IdempotencyConflict as exc:
            self._error(HTTPStatus.CONFLICT, "idempotency_conflict", str(exc))
        except (BridgeRequestError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--config", default="configs/agentteams/host-bridge-native-v1.json")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument(
        "--auth-gateway-url",
        default=os.environ.get("SCHEDNAV_BRIDGE_AUTH_GATEWAY_URL", ""),
        help="Optional trusted bearer-token validation endpoint, such as AgentTeams /v1/models",
    )
    args = parser.parse_args()
    project_root = Path(args.project_root).resolve()
    config_path = (project_root / args.config).resolve()
    tokens: list[str] = []
    direct_token = os.environ.get("SCHEDNAV_BRIDGE_TOKEN", "")
    if direct_token:
        tokens.append(direct_token)
    token_file = os.environ.get("SCHEDNAV_BRIDGE_TOKEN_FILE", "")
    if token_file:
        token_path = Path(token_file).resolve()
        if not token_path.is_file():
            raise SystemExit("SCHEDNAV_BRIDGE_TOKEN_FILE does not identify a file")
        tokens.extend(
            line.strip()
            for line in token_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    tokens = list(dict.fromkeys(tokens))
    if any(len(token) < 32 for token in tokens):
        raise SystemExit("Every direct bridge token must contain at least 32 characters")
    if not tokens and not args.auth_gateway_url:
        raise SystemExit("Provide a direct bridge token or --auth-gateway-url")
    catalog = BridgeCatalog.load(project_root, config_path)
    service = BridgeService(catalog)
    server = BridgeHTTPServer(
        (args.bind, args.port),
        service,
        tuple(tokens),
        args.auth_gateway_url or None,
    )
    print(
        json.dumps(
            {
                "service": "schednav-host-bridge",
                "bind": args.bind,
                "port": server.server_address[1],
                "max_workers": catalog.max_workers,
                "artifact_root": catalog.artifact_root.name,
                "accepted_identity_count": len(tokens),
                "delegated_auth": bool(args.auth_gateway_url),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
