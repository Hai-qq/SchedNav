"""Structured contracts for deterministic GFS reproduction runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any


SCHEMA_VERSION = "schednav.reproduction-run/v1"
ALIBABA_TRACE_ORIGIN = "2024-03-01 00:00:00"
SAFE_EXPERIMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _timestamp(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp, got {value!r}") from exc


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WindowSpec:
    submit_time_origin: str
    trace_end: str
    evaluation_start: str
    evaluation_end: str
    gpu_models: tuple[str, ...]
    drain_mode: str = "until_all_jobs_end"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WindowSpec":
        return cls(
            submit_time_origin=str(value["submit_time_origin"]),
            trace_end=str(value["trace_end"]),
            evaluation_start=str(value["evaluation_start"]),
            evaluation_end=str(value["evaluation_end"]),
            gpu_models=tuple(str(item) for item in value["gpu_models"]),
            drain_mode=str(value.get("drain_mode", "until_all_jobs_end")),
        )

    def validate(self) -> None:
        origin = _timestamp(self.submit_time_origin, "submit_time_origin")
        trace_end = _timestamp(self.trace_end, "trace_end")
        evaluation_start = _timestamp(self.evaluation_start, "evaluation_start")
        evaluation_end = _timestamp(self.evaluation_end, "evaluation_end")
        if self.submit_time_origin != ALIBABA_TRACE_ORIGIN:
            raise ValueError(
                "V1 requires the Alibaba submit_time origin to remain "
                f"{ALIBABA_TRACE_ORIGIN}; a later start needs a state snapshot."
            )
        if not origin <= evaluation_start <= evaluation_end <= trace_end:
            raise ValueError(
                "Expected submit_time_origin <= evaluation_start <= evaluation_end <= trace_end"
            )
        if not self.gpu_models or len(set(self.gpu_models)) != len(self.gpu_models):
            raise ValueError("gpu_models must be a non-empty list without duplicates")
        if self.drain_mode != "until_all_jobs_end":
            raise ValueError("V1 only supports drain_mode=until_all_jobs_end")

    def seconds_from_origin(self, value: str) -> int:
        origin = _timestamp(self.submit_time_origin, "submit_time_origin")
        return int((_timestamp(value, "window timestamp") - origin).total_seconds())


@dataclass(frozen=True)
class PolicySpec:
    scheduler: str
    guarantee_hours: tuple[int, ...]
    guarantee_rate: float
    ckpt_interval_seconds: int
    seq_len_hours: int
    pred_len_hours: int
    estimator_validation_days: int
    recorder_initial_skip_hours: int
    train_epochs: int
    num_workers: int
    seed: int
    device: str
    deterministic: bool

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PolicySpec":
        return cls(
            scheduler=str(value["scheduler"]),
            guarantee_hours=tuple(int(item) for item in value["guarantee_hours"]),
            guarantee_rate=float(value["guarantee_rate"]),
            ckpt_interval_seconds=int(value["ckpt_interval_seconds"]),
            seq_len_hours=int(value.get("seq_len_hours", 672)),
            pred_len_hours=int(value.get("pred_len_hours", 4)),
            estimator_validation_days=int(value.get("estimator_validation_days", 7)),
            recorder_initial_skip_hours=int(value.get("recorder_initial_skip_hours", 20)),
            train_epochs=int(value.get("train_epochs", 10)),
            num_workers=int(value.get("num_workers", 0)),
            seed=int(value.get("seed", 20260807)),
            device=str(value.get("device", "cpu")),
            deterministic=bool(value.get("deterministic", True)),
        )

    def validate(self) -> None:
        if self.scheduler not in {"fifo_spot", "spot_scheduler"}:
            raise ValueError(f"Unsupported V1 scheduler: {self.scheduler}")
        if (
            not self.guarantee_hours
            or any(hour <= 0 for hour in self.guarantee_hours)
            or len(set(self.guarantee_hours)) != len(self.guarantee_hours)
        ):
            raise ValueError("guarantee_hours must contain unique positive integers")
        if max(self.guarantee_hours) > self.pred_len_hours:
            raise ValueError("guarantee_hours cannot exceed pred_len_hours")
        if not 0.0 < self.guarantee_rate < 1.0:
            raise ValueError("guarantee_rate must be between 0 and 1")
        if self.ckpt_interval_seconds <= 0 or self.seq_len_hours <= 0 or self.pred_len_hours <= 0:
            raise ValueError("checkpoint and forecast lengths must be positive")
        if self.estimator_validation_days < 0 or self.recorder_initial_skip_hours < 0:
            raise ValueError("estimator history offsets cannot be negative")
        if self.train_epochs <= 0 or self.num_workers < 0:
            raise ValueError("train_epochs must be positive and num_workers cannot be negative")
        if self.device not in {"cpu", "cuda"}:
            raise ValueError("device must be cpu or cuda")

    @property
    def minimum_warmup_hours(self) -> int:
        return (
            self.recorder_initial_skip_hours
            + self.seq_len_hours
            + self.pred_len_hours
            + self.estimator_validation_days * 24
        )

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(asdict(self))


@dataclass(frozen=True)
class RunSpec:
    schema_version: str
    experiment_name: str
    gfs_dir: str
    gfs_commit: str
    gfs_patch: str
    source_trace_dir: str
    trace_commit: str
    python_executable: str
    artifacts_dir: str
    window: WindowSpec
    policy: PolicySpec

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunSpec":
        spec = cls(
            schema_version=str(value["schema_version"]),
            experiment_name=str(value["experiment_name"]),
            gfs_dir=str(value["gfs_dir"]),
            gfs_commit=str(value["gfs_commit"]),
            gfs_patch=str(value["gfs_patch"]),
            source_trace_dir=str(value["source_trace_dir"]),
            trace_commit=str(value["trace_commit"]),
            python_executable=str(value["python_executable"]),
            artifacts_dir=str(value["artifacts_dir"]),
            window=WindowSpec.from_dict(value["window"]),
            policy=PolicySpec.from_dict(value["policy"]),
        )
        spec.validate()
        return spec

    @classmethod
    def load(cls, path: Path) -> "RunSpec":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Expected schema_version={SCHEMA_VERSION}")
        if not SAFE_EXPERIMENT_NAME.fullmatch(self.experiment_name):
            raise ValueError("experiment_name must be path-safe")
        if not re.fullmatch(r"[0-9a-f]{40}", self.gfs_commit) or not re.fullmatch(
            r"[0-9a-f]{40}", self.trace_commit
        ):
            raise ValueError("gfs_commit and trace_commit must be full 40-character SHAs")
        self.window.validate()
        self.policy.validate()
        warmup_hours = self.window.seconds_from_origin(self.window.evaluation_start) // 3600
        if warmup_hours < self.policy.minimum_warmup_hours:
            raise ValueError(
                f"Warm-up is {warmup_hours}h, but the current estimator contract requires at least "
                f"{self.policy.minimum_warmup_hours}h"
            )

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(asdict(self))
