"""Tenant-aware probabilistic demand forecasting and Spot quota control.

This module is a first-party implementation of a tenant-decomposed linear
Gaussian forecaster.  PyTorch and the China business calendar are optional
runtime dependencies and are imported only when this controller is selected.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import random
from statistics import NormalDist
from typing import Any

from .contracts import canonical_sha256
from .native_trace import CanonicalTrace, TRACE_SCHEMA_V2, TraceJob
from .predictive_control import (
    CONTROL_REPORT_SCHEMA,
    DECISION_SCHEMA,
    FORECAST_SCHEMA,
    OBSERVATION_BUNDLE_SCHEMA,
    QUOTA_SCHEMA,
    SNAPSHOT_SCHEMA,
)


TENANT_CONTROLLER_SCHEMA = "schednav.tenant-predictive-controller/v1"
MODEL_ID = "tenant-linear-gaussian-v1"
EPSILON = 1e-9


class ForecastDependencyError(RuntimeError):
    """Raised when the optional trainable-forecast dependencies are absent."""


@dataclass(frozen=True)
class TenantPredictiveControllerConfig:
    """Versioned contract for the tenant-aware predictive control profile."""

    controller_id: str
    demand_sample_interval_seconds: int
    quota_update_interval_seconds: int
    aggregation_interval_seconds: int
    lookback_hours: int
    forecast_horizon_hours: int
    retrain_interval_seconds: int
    validation_hours: int
    training_stride_seconds: int
    guarantee_probability: float
    guarantee_horizons_hours: tuple[int, ...]
    business_calendar: str
    moving_average_window: int
    embedding_dimension: int
    attention_hidden_dimension: int
    train_epochs: int
    batch_size: int
    learning_rate: float
    early_stopping_patience: int
    random_seed: int
    nonzero_targets_only: bool
    initial_eta: float
    minimum_eta: float
    maximum_eta: float
    feedback_window_seconds: int
    queue_wait_threshold_seconds: int
    high_eviction_multiple: float
    low_eviction_multiple: float
    runtime_inventory_cap: bool
    model: str = MODEL_ID

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TenantPredictiveControllerConfig":
        required = {
            "schema_version",
            "controller_id",
            "model",
            "demand_sample_interval_seconds",
            "quota_update_interval_seconds",
            "aggregation_interval_seconds",
            "lookback_hours",
            "forecast_horizon_hours",
            "retrain_interval_seconds",
            "validation_hours",
            "training_stride_seconds",
            "guarantee_probability",
            "guarantee_horizons_hours",
            "business_calendar",
            "moving_average_window",
            "embedding_dimension",
            "attention_hidden_dimension",
            "train_epochs",
            "batch_size",
            "learning_rate",
            "early_stopping_patience",
            "random_seed",
            "nonzero_targets_only",
            "initial_eta",
            "minimum_eta",
            "maximum_eta",
            "feedback_window_seconds",
            "queue_wait_threshold_seconds",
            "high_eviction_multiple",
            "low_eviction_multiple",
            "runtime_inventory_cap",
        }
        if set(value) != required:
            raise ValueError(
                "Tenant predictive controller fields must be exactly "
                f"{sorted(required)}"
            )
        if value["schema_version"] != TENANT_CONTROLLER_SCHEMA:
            raise ValueError(f"Expected schema_version={TENANT_CONTROLLER_SCHEMA}")
        horizons = value["guarantee_horizons_hours"]
        if not isinstance(horizons, list):
            raise ValueError("guarantee_horizons_hours must be a list")
        config = cls(
            controller_id=str(value["controller_id"]),
            model=str(value["model"]),
            demand_sample_interval_seconds=int(
                value["demand_sample_interval_seconds"]
            ),
            quota_update_interval_seconds=int(
                value["quota_update_interval_seconds"]
            ),
            aggregation_interval_seconds=int(value["aggregation_interval_seconds"]),
            lookback_hours=int(value["lookback_hours"]),
            forecast_horizon_hours=int(value["forecast_horizon_hours"]),
            retrain_interval_seconds=int(value["retrain_interval_seconds"]),
            validation_hours=int(value["validation_hours"]),
            training_stride_seconds=int(value["training_stride_seconds"]),
            guarantee_probability=float(value["guarantee_probability"]),
            guarantee_horizons_hours=tuple(int(item) for item in horizons),
            business_calendar=str(value["business_calendar"]),
            moving_average_window=int(value["moving_average_window"]),
            embedding_dimension=int(value["embedding_dimension"]),
            attention_hidden_dimension=int(value["attention_hidden_dimension"]),
            train_epochs=int(value["train_epochs"]),
            batch_size=int(value["batch_size"]),
            learning_rate=float(value["learning_rate"]),
            early_stopping_patience=int(value["early_stopping_patience"]),
            random_seed=int(value["random_seed"]),
            nonzero_targets_only=bool(value["nonzero_targets_only"]),
            initial_eta=float(value["initial_eta"]),
            minimum_eta=float(value["minimum_eta"]),
            maximum_eta=float(value["maximum_eta"]),
            feedback_window_seconds=int(value["feedback_window_seconds"]),
            queue_wait_threshold_seconds=int(value["queue_wait_threshold_seconds"]),
            high_eviction_multiple=float(value["high_eviction_multiple"]),
            low_eviction_multiple=float(value["low_eviction_multiple"]),
            runtime_inventory_cap=bool(value["runtime_inventory_cap"]),
        )
        config.validate()
        return config

    @classmethod
    def load(cls, path: Path) -> "TenantPredictiveControllerConfig":
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Tenant predictive controller config must be an object")
        return cls.from_dict(value)

    def validate(self) -> None:
        if not self.controller_id:
            raise ValueError("controller_id cannot be empty")
        if self.model != MODEL_ID:
            raise ValueError(f"Only model={MODEL_ID} is supported")
        if self.demand_sample_interval_seconds <= 0:
            raise ValueError("demand_sample_interval_seconds must be positive")
        if (
            self.aggregation_interval_seconds
            % self.demand_sample_interval_seconds
            != 0
        ):
            raise ValueError("aggregation must be a multiple of demand sampling")
        if self.aggregation_interval_seconds != 3600:
            raise ValueError("tenant-predictive-controller/v1 requires hourly aggregation")
        if (
            self.quota_update_interval_seconds
            % self.demand_sample_interval_seconds
            != 0
            or self.training_stride_seconds % self.demand_sample_interval_seconds != 0
        ):
            raise ValueError("quota and training cadence must align with sampling")
        positive = (
            self.lookback_hours,
            self.forecast_horizon_hours,
            self.retrain_interval_seconds,
            self.validation_hours,
            self.moving_average_window,
            self.embedding_dimension,
            self.attention_hidden_dimension,
            self.train_epochs,
            self.batch_size,
            self.early_stopping_patience,
            self.feedback_window_seconds,
            self.queue_wait_threshold_seconds,
        )
        if any(item <= 0 for item in positive) or self.learning_rate <= 0:
            raise ValueError("model, training, and feedback dimensions must be positive")
        if self.moving_average_window % 2 == 0:
            raise ValueError("moving_average_window must be odd")
        if self.retrain_interval_seconds < self.quota_update_interval_seconds:
            raise ValueError("retraining cannot be more frequent than quota updates")
        if not 0.5 < self.guarantee_probability < 1.0:
            raise ValueError("guarantee_probability must be between 0.5 and 1")
        if (
            not self.guarantee_horizons_hours
            or tuple(sorted(set(self.guarantee_horizons_hours)))
            != self.guarantee_horizons_hours
            or self.guarantee_horizons_hours[0] <= 0
            or self.guarantee_horizons_hours[-1] > self.forecast_horizon_hours
        ):
            raise ValueError("guarantee horizons must be increasing and inside forecast")
        if self.business_calendar not in {"china", "weekday"}:
            raise ValueError("business_calendar must be china or weekday")
        if not 0 < self.minimum_eta <= self.initial_eta <= self.maximum_eta:
            raise ValueError("Expected 0 < minimum_eta <= initial_eta <= maximum_eta")
        if self.high_eviction_multiple < 1.0:
            raise ValueError("high_eviction_multiple must be at least one")
        if not 0.0 <= self.low_eviction_multiple <= 1.0:
            raise ValueError("low_eviction_multiple must be between zero and one")

    @property
    def minimum_training_hours(self) -> int:
        return self.lookback_hours + self.validation_hours + self.forecast_horizon_hours

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["schema_version"] = TENANT_CONTROLLER_SCHEMA
        value["guarantee_horizons_hours"] = list(self.guarantee_horizons_hours)
        return {"schema_version": value.pop("schema_version"), **value}

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self.to_dict())


def _optional_runtime() -> tuple[Any, Any, Any, Any]:
    try:
        import numpy as np
        import torch
        import torch.nn as nn
        import torch.nn.functional as functional
    except ImportError as exc:
        raise ForecastDependencyError(
            "tenant-linear-gaussian-v1 requires the optional 'forecast' dependencies; "
            "install SchedNav with pip install -e .[forecast]"
        ) from exc
    return np, torch, nn, functional


def _workday(value: datetime, provider: str) -> int:
    if provider == "weekday":
        return int(value.weekday() < 5)
    try:
        from chinese_calendar import is_workday
    except ImportError as exc:
        raise ForecastDependencyError(
            "business_calendar=china requires the optional 'forecast' dependencies"
        ) from exc
    return int(bool(is_workday(value)))


def _time_categories(origin: datetime, seconds: float, provider: str) -> tuple[int, int, int]:
    value = origin + timedelta(seconds=float(seconds))
    return (_workday(value, provider), value.weekday(), value.hour)


def _series_id(pool: str, tenant: str) -> str:
    return json.dumps([pool, tenant], ensure_ascii=False, separators=(",", ":"))


def _job_series(job: TraceJob) -> tuple[str, dict[str, str]]:
    tenant = job.tenant_id or "__aggregate__"
    pool = job.gpu_model
    return _series_id(pool, tenant), {
        "pool": pool,
        "cluster": "cluster",
        "tenant": tenant,
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def aggregate_independent_gaussians(
    components: list[tuple[float, float]],
) -> tuple[float, float]:
    """Aggregate independent tenant Normal distributions in one resource pool."""
    if not components:
        raise ValueError("At least one Gaussian component is required")
    mean = sum(float(component[0]) for component in components)
    sigma = math.sqrt(sum(float(component[1]) ** 2 for component in components))
    return mean, sigma


def feedback_eta(
    previous_eta: float,
    eviction_rate: float | None,
    maximum_queue_wait_seconds: float,
    config: TenantPredictiveControllerConfig,
) -> tuple[float, str]:
    """Apply the bounded eviction/queue feedback law used by the controller."""
    target = 1.0 - config.guarantee_probability
    eta = float(previous_eta)
    reason = "unchanged"
    if (
        eviction_rate is not None
        and eviction_rate >= config.high_eviction_multiple * target
    ):
        eta *= target / eviction_rate
        reason = "high_eviction"
    elif (
        eviction_rate is not None
        and eviction_rate <= config.low_eviction_multiple * target
        and maximum_queue_wait_seconds > config.queue_wait_threshold_seconds
    ):
        eta *= 1.5 - eviction_rate / target
        reason = "low_eviction_high_queue"
    eta = max(config.minimum_eta, min(config.maximum_eta, eta))
    return eta, reason


def quota_from_quantiles(
    *,
    capacity_gpus: float,
    guarantee_quantiles_gpus: list[float],
    horizon_hours: int,
    eta: float,
    idle_gpus: float,
    running_spot_gpus: float,
    runtime_inventory_cap: bool,
) -> tuple[int, float]:
    """Convert high-quantile HP forecasts into one executable Spot quota."""
    if horizon_hours <= 0 or len(guarantee_quantiles_gpus) < horizon_hours:
        return 0, 0.0
    free = min(
        capacity_gpus
        - max(
            0.0,
            min(capacity_gpus, math.floor(value + EPSILON)),
        )
        for value in guarantee_quantiles_gpus[:horizon_hours]
    )
    quota = free * eta
    if runtime_inventory_cap:
        quota = min(quota, idle_gpus + running_spot_gpus)
    return int(max(0.0, min(capacity_gpus, math.floor(quota + EPSILON)))), free


def _make_network(config: TenantPredictiveControllerConfig, category_sizes: tuple[int, int, int]) -> Any:
    _np, torch, nn, functional = _optional_runtime()

    class TenantLinearGaussianNetwork(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            width = config.embedding_dimension
            self.business_embeddings = nn.ModuleList(
                nn.Embedding(size, width) for size in category_sizes
            )
            self.time_embeddings = nn.ModuleList(
                [nn.Embedding(2, width), nn.Embedding(7, width), nn.Embedding(24, width)]
            )
            self.query = nn.Linear(width, config.attention_hidden_dimension)
            self.key = nn.Linear(width, config.attention_hidden_dimension)
            self.value = nn.Linear(width, width)
            feature_width = config.lookback_hours + 6 * width
            self.cycle_head = nn.Linear(feature_width, config.forecast_horizon_hours)
            self.trend_head = nn.Linear(feature_width, config.forecast_horizon_hours)
            self.scale_head = nn.Linear(feature_width, config.forecast_horizon_hours)
            initial = 1.0 / config.lookback_hours
            for layer in (self.cycle_head, self.trend_head, self.scale_head):
                nn.init.constant_(layer.weight, initial)

        def forward(self, history: Any, time_index: Any, business_index: Any) -> tuple[Any, Any]:
            batch, _length, series_count = history.shape
            half = (config.moving_average_window - 1) // 2
            channel_first = history.permute(0, 2, 1)
            padded = functional.pad(channel_first, (half, half), mode="replicate")
            trend = functional.avg_pool1d(
                padded, kernel_size=config.moving_average_window, stride=1
            ).permute(0, 2, 1)
            cycle = history - trend

            business_parts = [
                embedding(business_index[:, dimension])
                for dimension, embedding in enumerate(self.business_embeddings)
            ]
            business = torch.stack(business_parts, dim=1)
            business = business.unsqueeze(0).expand(batch, -1, -1, -1)
            query = self.query(business)
            key = self.key(business)
            value = self.value(business)
            logits = torch.matmul(query, key.transpose(-1, -2))
            attention = torch.softmax(logits, dim=-1) / math.sqrt(
                config.attention_hidden_dimension
            )
            attended = torch.matmul(attention, value).reshape(batch, series_count, -1)

            time_parts = [
                embedding(time_index[:, dimension])
                for dimension, embedding in enumerate(self.time_embeddings)
            ]
            time_features = torch.cat(time_parts, dim=-1).unsqueeze(1).expand(
                -1, series_count, -1
            )
            cycle_features = torch.cat(
                [cycle.permute(0, 2, 1), time_features, attended], dim=-1
            )
            trend_features = torch.cat(
                [trend.permute(0, 2, 1), time_features, attended], dim=-1
            )
            scale_features = torch.cat(
                [history.permute(0, 2, 1), time_features, attended], dim=-1
            )
            mean = self.cycle_head(cycle_features) + self.trend_head(trend_features)
            sigma = functional.softplus(self.scale_head(scale_features))
            return mean.permute(0, 2, 1), sigma.permute(0, 2, 1)

    return TenantLinearGaussianNetwork()


class TenantLinearGaussianEstimator:
    """Trainable tenant-series forecaster with Gaussian uncertainty."""

    def __init__(
        self,
        config: TenantPredictiveControllerConfig,
        time_origin: str,
    ) -> None:
        self.config = config
        self.origin = datetime.fromisoformat(time_origin)
        self.model: Any | None = None
        self.series: tuple[str, ...] = ()
        self.series_metadata: dict[str, dict[str, str]] = {}
        self.category_maps: tuple[dict[str, int], ...] = ({}, {}, {})
        self.business_index: Any | None = None
        self.trained_at: float | None = None
        self.training_generation = 0
        self.model_fingerprint: str | None = None
        self.training_summary: dict[str, Any] = {}

    def _prepare_matrix(
        self,
        history: list[tuple[float, dict[str, float]]],
        metadata: dict[str, dict[str, str]],
    ) -> tuple[Any, float]:
        np, _torch, _nn, _functional = _optional_runtime()
        if not history:
            raise ValueError("Demand history cannot be empty")
        interval = self.config.demand_sample_interval_seconds
        first = float(history[0][0])
        for index, (timestamp, _values) in enumerate(history):
            expected = first + index * interval
            if abs(float(timestamp) - expected) > EPSILON:
                raise ValueError("Demand history must use a complete regular sample grid")
        series = tuple(sorted(metadata))
        if not series:
            raise ValueError("At least one observed demand series is required")
        matrix = np.zeros((len(history), len(series)), dtype=np.float32)
        positions = {key: index for index, key in enumerate(series)}
        for row, (_timestamp, values) in enumerate(history):
            for key, demand in values.items():
                if key in positions:
                    matrix[row, positions[key]] = float(demand)
        self._ensure_model(series, metadata)
        return matrix, first

    def _ensure_model(
        self,
        series: tuple[str, ...],
        metadata: dict[str, dict[str, str]],
    ) -> None:
        np, torch, _nn, _functional = _optional_runtime()
        selected = {key: dict(metadata[key]) for key in series}
        category_values = (
            sorted({selected[key]["pool"] for key in series}),
            sorted({selected[key]["cluster"] for key in series}),
            sorted({selected[key]["tenant"] for key in series}),
        )
        maps = tuple(
            {value: index for index, value in enumerate(values)}
            for values in category_values
        )
        signature = (series, tuple(tuple(values) for values in category_values))
        previous_signature = (
            self.series,
            tuple(tuple(mapping) for mapping in self.category_maps),
        )
        if self.model is None or signature != previous_signature:
            torch.manual_seed(self.config.random_seed)
            torch.use_deterministic_algorithms(True, warn_only=True)
            self.model = _make_network(
                self.config, tuple(len(values) for values in category_values)
            ).to(torch.device("cpu"))
        self.series = series
        self.series_metadata = selected
        self.category_maps = maps
        indices = [
            [
                maps[0][selected[key]["pool"]],
                maps[1][selected[key]["cluster"]],
                maps[2][selected[key]["tenant"]],
            ]
            for key in series
        ]
        self.business_index = torch.from_numpy(np.asarray(indices, dtype=np.int64))

    def _aligned_hourly(self, matrix: Any) -> dict[int, Any]:
        np, _torch, _nn, _functional = _optional_runtime()
        points_per_hour = (
            self.config.aggregation_interval_seconds
            // self.config.demand_sample_interval_seconds
        )
        stride_points = (
            self.config.training_stride_seconds
            // self.config.demand_sample_interval_seconds
        )
        aligned: dict[int, Any] = {}
        for offset in range(0, points_per_hour, stride_points):
            usable = (len(matrix) - offset) // points_per_hour
            if usable <= 0:
                continue
            values = matrix[offset : offset + usable * points_per_hour]
            aligned[offset] = values.reshape(
                usable, points_per_hour, matrix.shape[1]
            ).max(axis=1)
        return aligned

    def _sample_arrays(
        self,
        starts: list[int],
        aligned: dict[int, Any],
        first_timestamp: float,
    ) -> tuple[Any, Any, Any]:
        np, _torch, _nn, _functional = _optional_runtime()
        points_per_hour = (
            self.config.aggregation_interval_seconds
            // self.config.demand_sample_interval_seconds
        )
        xs, ys, times = [], [], []
        for start in starts:
            offset = start % points_per_hour
            hour_index = start // points_per_hour
            hourly = aligned[offset]
            target_index = hour_index + self.config.lookback_hours
            xs.append(hourly[hour_index:target_index])
            ys.append(
                hourly[
                    target_index : target_index + self.config.forecast_horizon_hours
                ]
            )
            target_seconds = first_timestamp + (
                start + self.config.lookback_hours * points_per_hour
            ) * self.config.demand_sample_interval_seconds
            times.append(
                _time_categories(
                    self.origin, target_seconds, self.config.business_calendar
                )
            )
        return (
            np.stack(xs).astype(np.float32, copy=False),
            np.asarray(times, dtype=np.int64),
            np.stack(ys).astype(np.float32, copy=False),
        )

    def _loss(self, mean: Any, sigma: Any, target: Any) -> Any:
        _np, torch, _nn, _functional = _optional_runtime()
        sigma = sigma.clamp_min(torch.finfo(sigma.dtype).eps)
        if self.config.nonzero_targets_only:
            mask = target != 0
            if not bool(mask.any()):
                return None
            mean, sigma, target = mean[mask], sigma[mask], target[mask]
        return torch.mean(
            torch.log(sigma)
            + 0.5 * ((target - mean) / sigma) ** 2
            + 0.5 * math.log(2 * math.pi)
        )

    def fit(
        self,
        history: list[tuple[float, dict[str, float]]],
        metadata: dict[str, dict[str, str]],
        trained_at: float,
    ) -> dict[str, Any]:
        np, torch, _nn, _functional = _optional_runtime()
        matrix, first = self._prepare_matrix(history, metadata)
        points_per_hour = (
            self.config.aggregation_interval_seconds
            // self.config.demand_sample_interval_seconds
        )
        stride_points = (
            self.config.training_stride_seconds
            // self.config.demand_sample_interval_seconds
        )
        required_points = (
            self.config.lookback_hours + self.config.forecast_horizon_hours
        ) * points_per_hour
        starts = list(range(0, len(matrix) - required_points + 1, stride_points))
        validation_boundary = trained_at - self.config.validation_hours * 3600
        train_starts: list[int] = []
        validation_starts: list[int] = []
        for start in starts:
            target_start = first + (
                start + self.config.lookback_hours * points_per_hour
            ) * self.config.demand_sample_interval_seconds
            target_end = target_start + self.config.forecast_horizon_hours * 3600
            if target_end <= validation_boundary + EPSILON:
                train_starts.append(start)
            elif target_start >= validation_boundary - EPSILON and target_end <= trained_at + EPSILON:
                validation_starts.append(start)
        if not train_starts or not validation_starts:
            raise ValueError(
                "Insufficient history for lookback, training targets, and validation targets"
            )
        aligned = self._aligned_hourly(matrix)
        assert self.model is not None and self.business_index is not None
        optimizer = torch.optim.Adam(self.model.parameters(), lr=self.config.learning_rate)
        best_state = deepcopy(self.model.state_dict())
        best_validation = math.inf
        stale_epochs = 0
        epochs_completed = 0
        batch_size = self.config.batch_size
        for epoch in range(self.config.train_epochs):
            order = train_starts.copy()
            random.Random(
                self.config.random_seed + self.training_generation * 1000 + epoch
            ).shuffle(order)
            self.model.train()
            for offset in range(0, len(order), batch_size):
                batch = order[offset : offset + batch_size]
                x, time_index, target = self._sample_arrays(batch, aligned, first)
                optimizer.zero_grad()
                mean, sigma = self.model(
                    torch.from_numpy(x),
                    torch.from_numpy(time_index),
                    self.business_index,
                )
                loss = self._loss(mean, sigma, torch.from_numpy(target))
                if loss is None:
                    continue
                loss.backward()
                optimizer.step()

            self.model.eval()
            validation_losses: list[float] = []
            with torch.no_grad():
                for offset in range(0, len(validation_starts), batch_size):
                    batch = validation_starts[offset : offset + batch_size]
                    x, time_index, target = self._sample_arrays(batch, aligned, first)
                    mean, sigma = self.model(
                        torch.from_numpy(x),
                        torch.from_numpy(time_index),
                        self.business_index,
                    )
                    loss = self._loss(mean, sigma, torch.from_numpy(target))
                    if loss is not None and math.isfinite(float(loss.item())):
                        validation_losses.append(float(loss.item()))
            if not validation_losses:
                raise ValueError("Validation contains no usable targets")
            validation = _mean(validation_losses)
            epochs_completed = epoch + 1
            if validation + EPSILON < best_validation:
                best_validation = validation
                best_state = deepcopy(self.model.state_dict())
                stale_epochs = 0
            else:
                stale_epochs += 1
                if stale_epochs >= self.config.early_stopping_patience:
                    break
            completed_epoch = epoch + 1
            next_learning_rate = self.config.learning_rate * (
                0.5 ** ((completed_epoch - 1) // 3)
            )
            for group in optimizer.param_groups:
                group["lr"] = next_learning_rate
        self.model.load_state_dict(best_state)
        self.model.eval()
        self.trained_at = float(trained_at)
        self.training_generation += 1
        self.model_fingerprint = self._fingerprint_model()
        self.training_summary = {
            "trained_at_seconds": self.trained_at,
            "training_generation": self.training_generation,
            "series_count": len(self.series),
            "history_sample_count": len(history),
            "train_window_count": len(train_starts),
            "validation_window_count": len(validation_starts),
            "epochs_completed": epochs_completed,
            "best_validation_gaussian_nll": round(best_validation, 9),
        }
        return dict(self.training_summary)

    def _fingerprint_model(self) -> str:
        _np, _torch, _nn, _functional = _optional_runtime()
        assert self.model is not None
        digest = hashlib.sha256()
        metadata = {
            "model": MODEL_ID,
            "controller_fingerprint": self.config.fingerprint,
            "series": list(self.series),
            "series_metadata": self.series_metadata,
            "category_maps": self.category_maps,
            "trained_at_seconds": self.trained_at,
            "training_generation": self.training_generation,
        }
        digest.update(
            json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        for name, tensor in sorted(self.model.state_dict().items()):
            digest.update(name.encode("utf-8"))
            values = tensor.detach().cpu().contiguous().numpy()
            digest.update(str(values.dtype).encode("ascii"))
            digest.update(str(values.shape).encode("ascii"))
            digest.update(values.tobytes(order="C"))
        return digest.hexdigest()

    def forecast(
        self,
        history: list[tuple[float, dict[str, float]]],
        metadata: dict[str, dict[str, str]],
        cutoff_seconds: float,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        np, torch, _nn, _functional = _optional_runtime()
        if self.model is None or self.model_fingerprint is None or self.trained_at is None:
            raise RuntimeError("The tenant estimator must be fitted before forecasting")
        matrix, _first = self._prepare_matrix(history, metadata)
        points_per_hour = (
            self.config.aggregation_interval_seconds
            // self.config.demand_sample_interval_seconds
        )
        required = self.config.lookback_hours * points_per_hour
        if len(matrix) < required:
            raise ValueError("Forecast history is shorter than the configured lookback")
        recent = matrix[-required:].reshape(
            self.config.lookback_hours, points_per_hour, len(self.series)
        ).max(axis=1)
        time_index = np.asarray(
            [_time_categories(self.origin, cutoff_seconds, self.config.business_calendar)],
            dtype=np.int64,
        )
        assert self.business_index is not None
        self.model.eval()
        with torch.no_grad():
            mean, sigma = self.model(
                torch.from_numpy(recent[None, ...].astype(np.float32, copy=False)),
                torch.from_numpy(time_index),
                self.business_index,
            )
        means = mean[0].detach().cpu().numpy()
        sigmas = sigma[0].detach().cpu().numpy()
        z = NormalDist().inv_cdf(self.config.guarantee_probability)
        pool_steps: dict[str, list[dict[str, float]]] = {}
        for step in range(self.config.forecast_horizon_hours):
            by_pool: dict[str, list[int]] = {}
            for index, key in enumerate(self.series):
                by_pool.setdefault(self.series_metadata[key]["pool"], []).append(index)
            for pool, indices in by_pool.items():
                pool_mean, pool_sigma = aggregate_independent_gaussians(
                    [
                        (float(means[step, index]), float(sigmas[step, index]))
                        for index in indices
                    ]
                )
                pool_steps.setdefault(pool, []).append(
                    {
                        "horizon_step": step + 1,
                        "target_time_seconds": float(cutoff_seconds + (step + 1) * 3600),
                        "mu_gpus": round(pool_mean, 6),
                        "sigma_gpus": round(pool_sigma, 6),
                        "guarantee_quantile_gpus": round(pool_mean + z * pool_sigma, 6),
                    }
                )
        points = [
            {"resource_pool": pool, **point}
            for pool in sorted(pool_steps)
            for point in pool_steps[pool]
        ]
        return points, {
            "model": MODEL_ID,
            "model_fingerprint": self.model_fingerprint,
            "trained_at_seconds": self.trained_at,
            "series_count": len(self.series),
            "training": dict(self.training_summary),
            "aggregation": "tenant means sum; independent tenant variances sum",
        }


class TenantPredictiveSpotController:
    """Minute-observed, five-minute tenant-aware predictive quota controller."""

    resource_pool_scoped = True
    control_window_only = True
    periodic_guarantee_feedback = True

    def __init__(
        self,
        config: TenantPredictiveControllerConfig,
        capacity_gpus_by_pool: dict[str, float],
        start_time_seconds: float,
        time_origin: str,
        *,
        evidence_start_seconds: float | None = None,
        evidence_end_seconds: float | None = None,
    ) -> None:
        config.validate()
        if not capacity_gpus_by_pool or any(value <= 0 for value in capacity_gpus_by_pool.values()):
            raise ValueError("Every predictive resource pool requires positive capacity")
        self.config = config
        self.capacity_by_pool = {
            str(key): float(value) for key, value in sorted(capacity_gpus_by_pool.items())
        }
        self.capacity_gpus = sum(self.capacity_by_pool.values())
        self.estimator = TenantLinearGaussianEstimator(config, time_origin)
        self.next_update_time = float(start_time_seconds)
        self._next_quota_time = float(start_time_seconds)
        self._next_retrain_time: float | None = None
        self._history: list[tuple[float, dict[str, float]]] = []
        self._series_metadata: dict[str, dict[str, str]] = {}
        self._eta_by_pool = {
            pool: config.initial_eta for pool in self.capacity_by_pool
        }
        self._quota_by_pool: dict[str, dict[int, int]] = {
            pool: {horizon: 0 for horizon in config.guarantee_horizons_hours}
            for pool in self.capacity_by_pool
        }
        self._feedback: dict[str, list[tuple[float, bool, float]]] = {
            pool: [] for pool in self.capacity_by_pool
        }
        self._feedback_event_totals: dict[str, dict[str, Any]] = {
            pool: {
                "event_count": 0,
                "success_weight": 0.0,
                "failure_weight": 0.0,
                "weight_by_event_kind": {},
            }
            for pool in self.capacity_by_pool
        }
        self._latest: dict[str, Any] | None = None
        self._decisions: list[dict[str, Any]] = []
        self._all_decision_count = 0
        self._forecast_targets: list[tuple[float, str, float, float]] = []
        self._evidence_start = evidence_start_seconds
        self._evidence_end = evidence_end_seconds

    def bind_evidence_window(self, start_seconds: float, end_seconds: float) -> None:
        if end_seconds + EPSILON < start_seconds:
            raise ValueError("Evidence window end cannot precede its start")
        self._evidence_start = float(start_seconds)
        self._evidence_end = float(end_seconds)
        if self._next_retrain_time is None:
            self._next_retrain_time = float(start_seconds)

    def is_update_due(self, now: float) -> bool:
        return now + EPSILON >= self.next_update_time

    def _inside_evidence(self, now: float) -> bool:
        return (
            self._evidence_start is not None
            and self._evidence_end is not None
            and self._evidence_start - EPSILON <= now <= self._evidence_end + EPSILON
        )

    def _history_ready(self, now: float) -> bool:
        if not self._history:
            return False
        return (
            now - self._history[0][0]
            >= self.config.minimum_training_hours * 3600 - EPSILON
        )

    def _update_feedback(
        self,
        now: float,
        maximum_spot_queue_wait_seconds_by_pool: dict[str, float],
    ) -> dict[str, Any]:
        report: dict[str, Any] = {}
        for pool in self.capacity_by_pool:
            cutoff = now - self.config.feedback_window_seconds
            events = [event for event in self._feedback[pool] if event[0] >= cutoff]
            self._feedback[pool] = events
            total_weight = sum(event[2] for event in events)
            evicted_weight = sum(event[2] for event in events if event[1])
            rate = evicted_weight / total_weight if total_weight > 0 else None
            previous = self._eta_by_pool[pool]
            eta, reason = feedback_eta(
                previous,
                rate,
                maximum_spot_queue_wait_seconds_by_pool.get(pool, 0.0),
                self.config,
            )
            self._eta_by_pool[pool] = eta
            report[pool] = {
                "event_weight": round(total_weight, 6),
                "eviction_rate": round(rate, 9) if rate is not None else None,
                "eta_before": round(previous, 9),
                "eta_after": round(eta, 9),
                "reason": reason,
            }
        return report

    def update(
        self,
        now: float,
        *,
        hp_outstanding_requested_gpus: float,
        spot_backlog_gpus: float,
        running_spot_gpus: float,
        hp_running_requested_gpus_by_series: dict[str, float] | None = None,
        demand_series_metadata: dict[str, dict[str, str]] | None = None,
        spot_backlog_gpus_by_pool: dict[str, float] | None = None,
        running_spot_gpus_by_pool: dict[str, float] | None = None,
        idle_gpus_by_pool: dict[str, float] | None = None,
        maximum_spot_queue_wait_seconds_by_pool: dict[str, float] | None = None,
    ) -> dict[str, Any] | None:
        if not self.is_update_due(now):
            raise ValueError("Controller update was called before the next sample time")
        if self._evidence_end is not None and now > self._evidence_end + EPSILON:
            self.next_update_time = math.inf
            return None
        values = dict(hp_running_requested_gpus_by_series or {})
        metadata = demand_series_metadata or {}
        for key in values:
            if key not in metadata:
                raise ValueError("Every demand series requires structured metadata")
            pool = metadata[key].get("pool")
            if pool not in self.capacity_by_pool:
                raise ValueError("Demand series references an unknown resource pool")
            self._series_metadata[key] = dict(metadata[key])
        self._history.append((float(now), values))
        self.next_update_time = float(now + self.config.demand_sample_interval_seconds)
        if now + EPSILON < self._next_quota_time:
            return None
        self._next_quota_time = float(now + self.config.quota_update_interval_seconds)
        if self._evidence_start is not None and now + EPSILON < self._evidence_start:
            return None

        history_ready = self._history_ready(now) and bool(self._series_metadata)
        training: dict[str, Any] | None = None
        if history_ready and (
            self.estimator.trained_at is None
            or self._next_retrain_time is None
            or now + EPSILON >= self._next_retrain_time
            or tuple(sorted(self._series_metadata)) != self.estimator.series
        ):
            training = self.estimator.fit(
                self._history, self._series_metadata, float(now)
            )
            self._next_retrain_time = float(now + self.config.retrain_interval_seconds)

        queue_wait = maximum_spot_queue_wait_seconds_by_pool or {}
        feedback = self._update_feedback(now, queue_wait)
        forecast_points: list[dict[str, Any]] = []
        model_info: dict[str, Any] = {
            "model": MODEL_ID,
            "model_fingerprint": None,
            "trained_at_seconds": None,
            "series_count": len(self._series_metadata),
            "history_ready": history_ready,
        }
        if self.estimator.trained_at is not None:
            forecast_points, model_info = self.estimator.forecast(
                self._history, self._series_metadata, float(now)
            )
            model_info["history_ready"] = history_ready
        forecast: dict[str, Any] = {
            "schema_version": FORECAST_SCHEMA,
            "cutoff_time_seconds": float(now),
            "horizon_hours": self.config.forecast_horizon_hours,
            "guarantee_probability": self.config.guarantee_probability,
            "model": model_info,
            "points": forecast_points,
            "information_boundary": {
                "history_inclusive_cutoff_seconds": float(now),
                "actual_future_demand_used_for_prediction": False,
            },
        }
        forecast["forecast_fingerprint"] = canonical_sha256(forecast)

        by_pool: dict[str, list[dict[str, Any]]] = {}
        for point in forecast_points:
            by_pool.setdefault(str(point["resource_pool"]), []).append(point)
            if (
                self._evidence_end is None
                or float(point["target_time_seconds"])
                <= self._evidence_end + EPSILON
            ):
                self._forecast_targets.append(
                    (
                        float(point["target_time_seconds"]),
                        str(point["resource_pool"]),
                        float(point["mu_gpus"]),
                        float(point["guarantee_quantile_gpus"]),
                    )
                )
        running_by_pool = running_spot_gpus_by_pool or {}
        idle_by_pool = idle_gpus_by_pool or {}
        quotas_for_report: dict[str, dict[str, int]] = {}
        predicted_free: dict[str, dict[str, float]] = {}
        for pool, capacity in self.capacity_by_pool.items():
            points = sorted(by_pool.get(pool, []), key=lambda item: item["horizon_step"])
            pool_quotas: dict[str, int] = {}
            pool_free: dict[str, float] = {}
            for horizon in self.config.guarantee_horizons_hours:
                quota_value, free = quota_from_quantiles(
                    capacity_gpus=capacity,
                    guarantee_quantiles_gpus=[
                        float(point["guarantee_quantile_gpus"]) for point in points
                    ],
                    horizon_hours=horizon,
                    eta=self._eta_by_pool[pool],
                    idle_gpus=idle_by_pool.get(pool, capacity),
                    running_spot_gpus=running_by_pool.get(pool, 0.0),
                    runtime_inventory_cap=self.config.runtime_inventory_cap,
                )
                self._quota_by_pool[pool][horizon] = quota_value
                pool_quotas[str(horizon)] = quota_value
                pool_free[str(horizon)] = round(free, 6)
            quotas_for_report[pool] = pool_quotas
            predicted_free[pool] = pool_free
        aggregate_quotas = {
            str(horizon): sum(
                self._quota_by_pool[pool][horizon] for pool in self.capacity_by_pool
            )
            for horizon in self.config.guarantee_horizons_hours
        }
        quota_plan: dict[str, Any] = {
            "schema_version": QUOTA_SCHEMA,
            "cutoff_time_seconds": float(now),
            "forecast_fingerprint": forecast["forecast_fingerprint"],
            "eta": round(min(self._eta_by_pool.values()), 9),
            "eta_by_resource_pool": {
                key: round(value, 9) for key, value in self._eta_by_pool.items()
            },
            "predicted_free_gpus_by_resource_pool_and_guarantee_hour": predicted_free,
            "spot_quota_gpus_by_guarantee_hour": aggregate_quotas,
            "spot_quota_gpus_by_resource_pool_and_guarantee_hour": quotas_for_report,
            "runtime_inventory_cap": self.config.runtime_inventory_cap,
            "rule": "tenant Gaussian aggregation, horizon minimum, eta, runtime inventory cap",
        }
        quota_plan["quota_plan_fingerprint"] = canonical_sha256(quota_plan)
        snapshot: dict[str, Any] = {
            "schema_version": SNAPSHOT_SCHEMA,
            "cutoff_time_seconds": float(now),
            "history": {
                "sample_interval_seconds": self.config.demand_sample_interval_seconds,
                "sample_count": len(self._history),
                "first_sample_time_seconds": self._history[0][0],
                "last_sample_time_seconds": self._history[-1][0],
                "series_count": len(self._series_metadata),
                "history_ready": history_ready,
            },
            "current_state": {
                "hp_outstanding_requested_gpus": round(
                    float(hp_outstanding_requested_gpus), 6
                ),
                "hp_running_requested_gpus": round(sum(values.values()), 6),
                "spot_backlog_gpus": round(float(spot_backlog_gpus), 6),
                "running_spot_gpus": round(float(running_spot_gpus), 6),
                "spot_backlog_gpus_by_resource_pool": spot_backlog_gpus_by_pool or {},
                "running_spot_gpus_by_resource_pool": running_by_pool,
                "idle_gpus_by_resource_pool": idle_by_pool,
                "maximum_spot_queue_wait_seconds_by_resource_pool": queue_wait,
            },
            "information_boundary": {
                "actual_future_demand_used_for_prediction": False,
                "scheduler_observations_not_trace_future": True,
            },
        }
        snapshot["snapshot_fingerprint"] = canonical_sha256(snapshot)
        decision = {
            "schema_version": DECISION_SCHEMA,
            "cutoff_time_seconds": float(now),
            "snapshot_fingerprint": snapshot["snapshot_fingerprint"],
            "forecast_fingerprint": forecast["forecast_fingerprint"],
            "quota_plan_fingerprint": quota_plan["quota_plan_fingerprint"],
            "model_fingerprint": model_info.get("model_fingerprint"),
            "trained_at_seconds": model_info.get("trained_at_seconds"),
            "training_performed": training is not None,
            "history_ready": history_ready,
            "feedback_by_resource_pool": feedback,
            "eta": quota_plan["eta"],
            "eta_by_resource_pool": quota_plan["eta_by_resource_pool"],
            "spot_quota_gpus_by_guarantee_hour": aggregate_quotas,
            "spot_quota_gpus_by_resource_pool_and_guarantee_hour": quotas_for_report,
        }
        decision["decision_fingerprint"] = canonical_sha256(decision)
        self._latest = {
            "snapshot": snapshot,
            "forecast": forecast,
            "quota_plan": quota_plan,
            "decision": decision,
        }
        self._all_decision_count += 1
        if self._inside_evidence(now):
            self._decisions.append(decision)
        return self._latest

    def observe_spot_run_end(
        self,
        now: float,
        *,
        evicted: bool,
        resource_pool: str | None = None,
        event_weight: float = 1.0,
        guarantee_seconds: int | None = None,
        event_kind: str | None = None,
    ) -> None:
        if resource_pool not in self._feedback:
            raise ValueError("Predictive feedback requires a known resource pool")
        if event_weight <= 0:
            raise ValueError("Predictive feedback event_weight must be positive")
        pool = resource_pool
        self._feedback[pool].append((float(now), bool(evicted), float(event_weight)))
        totals = self._feedback_event_totals[pool]
        totals["event_count"] += 1
        outcome_key = "failure_weight" if evicted else "success_weight"
        totals[outcome_key] += float(event_weight)
        kind = event_kind or ("preempted" if evicted else "run_completed")
        by_kind = totals["weight_by_event_kind"]
        by_kind[kind] = by_kind.get(kind, 0.0) + float(event_weight)

    def quota_for_guarantee_seconds(
        self, guarantee_seconds: int, resource_pool: str | None = None
    ) -> int:
        hours = int(math.ceil(guarantee_seconds / 3600))
        horizon = next(
            (item for item in self.config.guarantee_horizons_hours if item >= hours),
            None,
        )
        if horizon is None:
            raise ValueError("Policy guarantee exceeds the controller forecast horizon")
        if resource_pool is None or resource_pool == "*":
            return sum(self._quota_by_pool[pool][horizon] for pool in self.capacity_by_pool)
        if resource_pool not in self._quota_by_pool:
            raise ValueError("Spot job references an unknown predictive resource pool")
        return self._quota_by_pool[resource_pool][horizon]

    def allows_spot(
        self,
        requested_gpus: float,
        running_spot_gpus: float,
        guarantee_seconds: int,
        *,
        resource_pool: str | None = None,
    ) -> bool:
        quota = self.quota_for_guarantee_seconds(guarantee_seconds, resource_pool)
        return running_spot_gpus + requested_gpus <= quota + EPSILON

    @property
    def latest(self) -> dict[str, Any] | None:
        return self._latest

    def finalize(self) -> dict[str, Any]:
        actual_by_time_pool: dict[tuple[float, str], float] = {}
        for timestamp, values in self._history:
            by_pool: dict[str, float] = {}
            for key, demand in values.items():
                metadata = self._series_metadata.get(key)
                if metadata is not None:
                    pool = metadata["pool"]
                    by_pool[pool] = by_pool.get(pool, 0.0) + float(demand)
            for pool, demand in by_pool.items():
                actual_by_time_pool[(timestamp, pool)] = demand
        errors: list[float] = []
        absolute_errors: list[float] = []
        actuals: list[float] = []
        coverage: list[bool] = []
        pinball: list[float] = []
        probability = self.config.guarantee_probability
        for target, pool, mean, quantile in self._forecast_targets:
            actual = actual_by_time_pool.get((target, pool))
            if actual is None:
                continue
            error = mean - actual
            errors.append(error)
            absolute_errors.append(abs(error))
            actuals.append(actual)
            coverage.append(actual <= quantile + EPSILON)
            residual = actual - quantile
            pinball.append(
                probability * residual if residual >= 0 else (probability - 1) * residual
            )
        eta_values = [
            float(value)
            for decision in self._decisions
            for value in decision["eta_by_resource_pool"].values()
        ]
        quota_values = [
            int(value)
            for decision in self._decisions
            for quotas in decision[
                "spot_quota_gpus_by_resource_pool_and_guarantee_hour"
            ].values()
            for value in quotas.values()
        ]
        report: dict[str, Any] = {
            "schema_version": CONTROL_REPORT_SCHEMA,
            "controller_id": self.config.controller_id,
            "controller": self.config.to_dict(),
            "controller_fingerprint": self.config.fingerprint,
            "information_boundary": {
                "scheduler_state_and_past_history_only": True,
                "actual_future_demand_used_for_prediction": False,
                "forecast_scoring_occurs_after_target_observation": True,
                "tenant_categories_registered_only_after_observation": True,
            },
            "evidence_window_seconds": {
                "start": self._evidence_start,
                "end": self._evidence_end,
            },
            "update_count": len(self._decisions),
            "total_runtime_update_count": self._all_decision_count,
            "demand_sample_count": len(self._history),
            "first_cutoff_time_seconds": (
                self._decisions[0]["cutoff_time_seconds"] if self._decisions else None
            ),
            "last_cutoff_time_seconds": (
                self._decisions[-1]["cutoff_time_seconds"] if self._decisions else None
            ),
            "decisions": self._decisions,
            "forecast_evaluation": {
                "scored_point_count": len(errors),
                "mae_gpus": round(_mean(absolute_errors), 6) if errors else None,
                "wape": (
                    round(sum(absolute_errors) / sum(actuals), 9)
                    if actuals and sum(actuals) > 0
                    else None
                ),
                "mean_error_gpus": round(_mean(errors), 6) if errors else None,
                "guarantee_quantile_coverage": round(
                    sum(coverage) / len(coverage), 9
                )
                if coverage
                else None,
                "pinball_loss": round(_mean(pinball), 6) if pinball else None,
            },
            "eta": {
                "initial": self.config.initial_eta,
                "final_by_resource_pool": {
                    key: round(value, 9) for key, value in self._eta_by_pool.items()
                },
                "minimum_observed": min(eta_values) if eta_values else None,
                "maximum_observed": max(eta_values) if eta_values else None,
            },
            "spot_quota_gpus": {
                "mean": round(_mean([float(value) for value in quota_values]), 6)
                if quota_values
                else None,
                "minimum": min(quota_values) if quota_values else None,
                "maximum": max(quota_values) if quota_values else None,
                "by_resource_pool": self._quota_by_pool,
            },
            "feedback_events": {
                pool: {
                    "event_count": values["event_count"],
                    "success_weight": round(values["success_weight"], 6),
                    "failure_weight": round(values["failure_weight"], 6),
                    "total_weight": round(
                        values["success_weight"] + values["failure_weight"], 6
                    ),
                    "weight_by_event_kind": {
                        key: round(weight, 6)
                        for key, weight in sorted(
                            values["weight_by_event_kind"].items()
                        )
                    },
                }
                for pool, values in self._feedback_event_totals.items()
            },
            "model": {
                "model": MODEL_ID,
                "model_fingerprint": self.estimator.model_fingerprint,
                "trained_at_seconds": self.estimator.trained_at,
                "training": self.estimator.training_summary,
            },
        }
        report["control_fingerprint"] = canonical_sha256(report)
        return report


def _pool_capacities(trace: CanonicalTrace) -> dict[str, float]:
    capacities: dict[str, float] = {}
    for node in trace.nodes:
        capacities[node.gpu_model] = capacities.get(node.gpu_model, 0.0) + node.gpu_count
    return capacities


def validate_tenant_predictive_trace(trace: CanonicalTrace) -> None:
    """Reject traces that would silently collapse tenant or pool dimensions."""
    if trace.schema_version != TRACE_SCHEMA_V2:
        raise ValueError(
            "tenant-linear-gaussian-v1 requires schednav.trace/v2 with tenant_id"
        )
    if any(not job.tenant_id for job in trace.jobs):
        raise ValueError("Every job requires tenant_id for tenant-aware prediction")
    if any(job.gpu_model == "*" for job in trace.jobs):
        raise ValueError(
            "tenant-linear-gaussian-v1 requires a concrete gpu_model resource pool"
        )


def build_tenant_observation_bundle(
    trace: CanonicalTrace,
    config: TenantPredictiveControllerConfig,
    cutoff_seconds: float,
) -> dict[str, Any]:
    """Build a cutoff-safe offline observation bundle for the tenant controller."""
    validate_tenant_predictive_trace(trace)
    if cutoff_seconds < min(job.submit_time_seconds for job in trace.jobs):
        raise ValueError("cutoff_seconds precedes the first trace arrival")
    interval = config.demand_sample_interval_seconds
    start = min(job.submit_time_seconds for job in trace.jobs)
    start = math.floor(start / interval) * interval
    if abs(cutoff_seconds / interval - round(cutoff_seconds / interval)) > EPSILON:
        raise ValueError("cutoff_seconds must align with demand sampling")
    if (
        abs(
            (cutoff_seconds - start) / config.quota_update_interval_seconds
            - round(
                (cutoff_seconds - start) / config.quota_update_interval_seconds
            )
        )
        > EPSILON
    ):
        raise ValueError("cutoff_seconds must align with quota updates")
    prefix = [job for job in trace.jobs if job.submit_time_seconds <= cutoff_seconds]
    controller = TenantPredictiveSpotController(
        config,
        _pool_capacities(trace),
        start,
        trace.time_origin,
        evidence_start_seconds=cutoff_seconds,
        evidence_end_seconds=cutoff_seconds,
    )
    events: list[tuple[float, int, TraceJob]] = []
    metadata: dict[str, dict[str, str]] = {}
    for job in prefix:
        events.append((job.submit_time_seconds, 1, job))
        if job.submit_time_seconds + job.duration_seconds <= cutoff_seconds + EPSILON:
            events.append((job.submit_time_seconds + job.duration_seconds, -1, job))
    events.sort(key=lambda item: (item[0], item[1], item[2].job_id))
    active_hp: dict[str, float] = {}
    active_spot_by_pool: dict[str, float] = {}
    capacities = _pool_capacities(trace)
    event_index = 0
    latest: dict[str, Any] | None = None
    sample = float(start)
    while sample <= cutoff_seconds + EPSILON:
        while event_index < len(events) and events[event_index][0] <= sample + EPSILON:
            _time, direction, job = events[event_index]
            if job.service_class == "HP":
                key, series_metadata = _job_series(job)
                metadata[key] = series_metadata
                active_hp[key] = active_hp.get(key, 0.0) + direction * job.gpu_count
                if active_hp[key] <= EPSILON:
                    active_hp[key] = 0.0
            else:
                active_spot_by_pool[job.gpu_model] = (
                    active_spot_by_pool.get(job.gpu_model, 0.0)
                    + direction * job.gpu_count
                )
                if active_spot_by_pool[job.gpu_model] <= EPSILON:
                    active_spot_by_pool[job.gpu_model] = 0.0
            event_index += 1
        hp_by_pool: dict[str, float] = {}
        for key, demand in active_hp.items():
            pool = metadata[key]["pool"]
            hp_by_pool[pool] = hp_by_pool.get(pool, 0.0) + demand
        idle = {
            pool: max(
                0.0,
                capacity
                - hp_by_pool.get(pool, 0.0)
                - active_spot_by_pool.get(pool, 0.0),
            )
            for pool, capacity in capacities.items()
        }
        decision = controller.update(
            sample,
            hp_outstanding_requested_gpus=sum(active_hp.values()),
            spot_backlog_gpus=0.0,
            running_spot_gpus=sum(active_spot_by_pool.values()),
            hp_running_requested_gpus_by_series=active_hp,
            demand_series_metadata=metadata,
            spot_backlog_gpus_by_pool={},
            running_spot_gpus_by_pool=active_spot_by_pool,
            idle_gpus_by_pool=idle,
            maximum_spot_queue_wait_seconds_by_pool={},
        )
        if decision is not None:
            latest = decision
        sample += interval
    if latest is None or latest["decision"]["cutoff_time_seconds"] != cutoff_seconds:
        raise RuntimeError("No predictive decision was produced at the requested cutoff")
    observed_jobs = [
        {
            "job_id": job.job_id,
            "submit_time_seconds": job.submit_time_seconds,
            "duration_seconds": job.duration_seconds,
            "gpu_count": job.gpu_count,
            "service_class": job.service_class,
            "gpu_model": job.gpu_model,
            "tenant_id": job.tenant_id,
        }
        for job in prefix
    ]
    observed_prefix_fingerprint = canonical_sha256(
        {
            "trace_id": trace.trace_id,
            "time_origin": trace.time_origin,
            "nodes": [asdict(node) for node in trace.nodes],
            "jobs": observed_jobs,
            "cutoff_time_seconds": cutoff_seconds,
        }
    )
    information_boundary = {
        "actual_future_demand_used_for_prediction": False,
        "jobs_with_submit_time_after_cutoff_excluded": True,
        "full_trace_fingerprint_exposed": False,
        "offline_state_approximation": "submit-to-submit-plus-duration occupancy",
    }
    bundle: dict[str, Any] = {
        "schema_version": OBSERVATION_BUNDLE_SCHEMA,
        "trace_id": trace.trace_id,
        "cutoff_time_seconds": float(cutoff_seconds),
        "controller_id": config.controller_id,
        "controller_fingerprint": config.fingerprint,
        "observed_prefix_fingerprint": observed_prefix_fingerprint,
        "observation_snapshot": latest["snapshot"],
        "demand_forecast": latest["forecast"],
        "spot_quota_plan": latest["quota_plan"],
        "information_boundary": information_boundary,
    }
    bundle["observation_bundle_fingerprint"] = canonical_sha256(bundle)
    return bundle
