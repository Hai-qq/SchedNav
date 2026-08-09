"""Past-only probabilistic demand forecasting and predictive Spot admission control."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from statistics import NormalDist
from typing import Any

from .contracts import canonical_sha256
from .native_trace import CanonicalTrace


CONTROLLER_SCHEMA = "schednav.predictive-controller/v1"
SNAPSHOT_SCHEMA = "schednav.observation-snapshot/v1"
FORECAST_SCHEMA = "schednav.demand-forecast/v1"
QUOTA_SCHEMA = "schednav.spot-quota-plan/v1"
DECISION_SCHEMA = "schednav.predictive-control-decision/v1"
CONTROL_REPORT_SCHEMA = "schednav.predictive-control-report/v1"
OBSERVATION_BUNDLE_SCHEMA = "schednav.predictive-observation-bundle/v1"
MODEL_ID = "seasonal-gaussian-v1"
EPSILON = 1e-9


@dataclass(frozen=True)
class PredictiveControllerConfig:
    """Versioned controls for the deterministic forecast/quota inner loop."""

    controller_id: str
    observation_interval_seconds: int
    aggregation_interval_seconds: int
    lookback_hours: int
    forecast_horizon_hours: int
    retrain_interval_seconds: int
    guarantee_probability: float
    guarantee_horizons_hours: tuple[int, ...]
    minimum_history_hours: int
    minimum_sigma_gpus: float
    initial_eta: float
    minimum_eta: float
    maximum_eta: float
    feedback_window_seconds: int
    starvation_increase_after_seconds: int
    high_eviction_ratio: float
    low_eviction_ratio: float
    eta_increase_step: float
    model: str = MODEL_ID

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PredictiveControllerConfig":
        required = {
            "schema_version",
            "controller_id",
            "model",
            "observation_interval_seconds",
            "aggregation_interval_seconds",
            "lookback_hours",
            "forecast_horizon_hours",
            "retrain_interval_seconds",
            "guarantee_probability",
            "guarantee_horizons_hours",
            "minimum_history_hours",
            "minimum_sigma_gpus",
            "initial_eta",
            "minimum_eta",
            "maximum_eta",
            "feedback_window_seconds",
            "starvation_increase_after_seconds",
            "high_eviction_ratio",
            "low_eviction_ratio",
            "eta_increase_step",
        }
        if set(value) != required:
            raise ValueError(
                "Predictive controller fields must be exactly " f"{sorted(required)}"
            )
        if value["schema_version"] != CONTROLLER_SCHEMA:
            raise ValueError(f"Expected schema_version={CONTROLLER_SCHEMA}")
        raw_horizons = value["guarantee_horizons_hours"]
        if not isinstance(raw_horizons, list):
            raise ValueError("guarantee_horizons_hours must be a list")
        config = cls(
            controller_id=str(value["controller_id"]),
            model=str(value["model"]),
            observation_interval_seconds=int(value["observation_interval_seconds"]),
            aggregation_interval_seconds=int(value["aggregation_interval_seconds"]),
            lookback_hours=int(value["lookback_hours"]),
            forecast_horizon_hours=int(value["forecast_horizon_hours"]),
            retrain_interval_seconds=int(value["retrain_interval_seconds"]),
            guarantee_probability=float(value["guarantee_probability"]),
            guarantee_horizons_hours=tuple(int(item) for item in raw_horizons),
            minimum_history_hours=int(value["minimum_history_hours"]),
            minimum_sigma_gpus=float(value["minimum_sigma_gpus"]),
            initial_eta=float(value["initial_eta"]),
            minimum_eta=float(value["minimum_eta"]),
            maximum_eta=float(value["maximum_eta"]),
            feedback_window_seconds=int(value["feedback_window_seconds"]),
            starvation_increase_after_seconds=int(
                value["starvation_increase_after_seconds"]
            ),
            high_eviction_ratio=float(value["high_eviction_ratio"]),
            low_eviction_ratio=float(value["low_eviction_ratio"]),
            eta_increase_step=float(value["eta_increase_step"]),
        )
        config.validate()
        return config

    @classmethod
    def load(cls, path: Path) -> "PredictiveControllerConfig":
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Predictive controller config must be a JSON object")
        return cls.from_dict(value)

    def validate(self) -> None:
        if not self.controller_id:
            raise ValueError("controller_id cannot be empty")
        if self.model != MODEL_ID:
            raise ValueError(f"Only model={MODEL_ID} is supported")
        if self.observation_interval_seconds <= 0:
            raise ValueError("observation_interval_seconds must be positive")
        if (
            self.aggregation_interval_seconds <= 0
            or self.aggregation_interval_seconds % self.observation_interval_seconds != 0
        ):
            raise ValueError(
                "aggregation_interval_seconds must be a positive multiple of the observation interval"
            )
        if self.aggregation_interval_seconds != 3600:
            raise ValueError("predictive-controller/v1 requires hourly aggregation")
        if self.lookback_hours <= 0 or self.forecast_horizon_hours <= 0:
            raise ValueError("lookback_hours and forecast_horizon_hours must be positive")
        if self.retrain_interval_seconds < self.aggregation_interval_seconds:
            raise ValueError("retrain_interval_seconds cannot be shorter than aggregation")
        if not 0.5 < self.guarantee_probability < 1.0:
            raise ValueError("guarantee_probability must be between 0.5 and 1")
        if (
            not self.guarantee_horizons_hours
            or tuple(sorted(set(self.guarantee_horizons_hours)))
            != self.guarantee_horizons_hours
            or self.guarantee_horizons_hours[0] <= 0
            or self.guarantee_horizons_hours[-1] > self.forecast_horizon_hours
        ):
            raise ValueError(
                "guarantee_horizons_hours must be unique, increasing, positive, and inside the forecast horizon"
            )
        if not 1 <= self.minimum_history_hours <= self.lookback_hours:
            raise ValueError("minimum_history_hours must be inside the lookback")
        if self.minimum_sigma_gpus < 0:
            raise ValueError("minimum_sigma_gpus cannot be negative")
        if not 0 < self.minimum_eta <= self.initial_eta <= self.maximum_eta:
            raise ValueError("Expected 0 < minimum_eta <= initial_eta <= maximum_eta")
        if self.feedback_window_seconds <= 0:
            raise ValueError("feedback_window_seconds must be positive")
        if self.starvation_increase_after_seconds <= 0:
            raise ValueError("starvation_increase_after_seconds must be positive")
        if not 1.0 <= self.high_eviction_ratio:
            raise ValueError("high_eviction_ratio must be at least 1")
        if not 0.0 <= self.low_eviction_ratio <= 1.0:
            raise ValueError("low_eviction_ratio must be between 0 and 1")
        if not 0.0 < self.eta_increase_step <= 1.0:
            raise ValueError("eta_increase_step must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["schema_version"] = CONTROLLER_SCHEMA
        value["guarantee_horizons_hours"] = list(self.guarantee_horizons_hours)
        return {"schema_version": value.pop("schema_version"), **value}

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self.to_dict())


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _population_sigma(values: list[float], mean: float) -> float:
    if not values:
        return 0.0
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


class _SeasonalGaussianEstimator:
    """Small deterministic trend/seasonality model with Gaussian uncertainty."""

    def __init__(self, config: PredictiveControllerConfig, capacity_gpus: float) -> None:
        self.config = config
        self.capacity_gpus = float(capacity_gpus)
        self._global_mean = 0.0
        self._sigma = config.minimum_sigma_gpus
        self._hour_of_day: dict[int, float] = {}
        self._hour_of_week: dict[int, float] = {}
        self._trend_per_hour = 0.0
        self._trained_at: float | None = None
        self._history_count = 0
        self._model_fingerprint: str | None = None

    @staticmethod
    def _bucket_number(timestamp: float, interval: int) -> int:
        return int(math.floor(timestamp / interval + EPSILON))

    def fit(self, history: list[tuple[float, float]], trained_at: float) -> None:
        if not history:
            history = [(trained_at, 0.0)]
        values = [float(value) for _timestamp, value in history]
        self._global_mean = _mean(values)
        by_day: dict[int, list[float]] = {}
        by_week: dict[int, list[float]] = {}
        for timestamp, value in history:
            bucket = self._bucket_number(
                timestamp, self.config.aggregation_interval_seconds
            )
            by_day.setdefault(bucket % 24, []).append(float(value))
            by_week.setdefault(bucket % 168, []).append(float(value))
        self._hour_of_day = {key: _mean(items) for key, items in by_day.items()}
        self._hour_of_week = {
            key: _mean(items) for key, items in by_week.items() if len(items) >= 2
        }
        residuals = []
        for timestamp, value in history:
            bucket = self._bucket_number(
                timestamp, self.config.aggregation_interval_seconds
            )
            expected = self._hour_of_week.get(
                bucket % 168,
                self._hour_of_day.get(bucket % 24, self._global_mean),
            )
            residuals.append(float(value) - expected)
        residual_mean = _mean(residuals)
        self._sigma = max(
            self.config.minimum_sigma_gpus,
            _population_sigma(residuals, residual_mean),
        )
        recent = values[-24:]
        previous = values[-48:-24]
        raw_trend = (_mean(recent) - _mean(previous)) / 24 if previous else 0.0
        trend_limit = max(1.0, self.capacity_gpus * 0.05)
        self._trend_per_hour = max(-trend_limit, min(trend_limit, raw_trend))
        self._trained_at = float(trained_at)
        self._history_count = len(history)
        model_payload = {
            "model": MODEL_ID,
            "trained_at_seconds": self._trained_at,
            "history_point_count": self._history_count,
            "global_mean": round(self._global_mean, 9),
            "sigma": round(self._sigma, 9),
            "trend_per_hour": round(self._trend_per_hour, 9),
            "hour_of_day": {
                str(key): round(value, 9) for key, value in sorted(self._hour_of_day.items())
            },
            "hour_of_week": {
                str(key): round(value, 9) for key, value in sorted(self._hour_of_week.items())
            },
        }
        self._model_fingerprint = canonical_sha256(model_payload)

    def forecast(
        self,
        history: list[tuple[float, float]],
        cutoff_seconds: float,
    ) -> tuple[list[dict[str, float]], dict[str, Any]]:
        if self._trained_at is None or self._model_fingerprint is None:
            raise RuntimeError("Estimator must be fitted before forecasting")
        recent = history[-24:]
        offsets: list[float] = []
        for timestamp, actual in recent:
            bucket = self._bucket_number(
                timestamp, self.config.aggregation_interval_seconds
            )
            expected = self._hour_of_week.get(
                bucket % 168,
                self._hour_of_day.get(bucket % 24, self._global_mean),
            )
            offsets.append(float(actual) - expected)
        level_offset = _mean(offsets)
        z = NormalDist().inv_cdf(self.config.guarantee_probability)
        points: list[dict[str, float]] = []
        for step in range(1, self.config.forecast_horizon_hours + 1):
            target = cutoff_seconds + step * self.config.aggregation_interval_seconds
            bucket = self._bucket_number(target, self.config.aggregation_interval_seconds)
            seasonal = self._hour_of_week.get(
                bucket % 168,
                self._hour_of_day.get(bucket % 24, self._global_mean),
            )
            mu = max(
                0.0,
                min(
                    self.capacity_gpus,
                    seasonal + level_offset + self._trend_per_hour * step,
                ),
            )
            sigma = self._sigma
            reserved = max(0.0, min(self.capacity_gpus, mu + z * sigma))
            points.append(
                {
                    "horizon_step": step,
                    "target_time_seconds": float(target),
                    "mu_gpus": round(mu, 6),
                    "sigma_gpus": round(sigma, 6),
                    "guarantee_quantile_gpus": round(reserved, 6),
                }
            )
        return points, {
            "model": MODEL_ID,
            "model_fingerprint": self._model_fingerprint,
            "trained_at_seconds": self._trained_at,
            "training_history_point_count": self._history_count,
        }


class PredictiveSpotController:
    """Stateful online controller that never accepts future workload records."""

    resource_pool_scoped = False

    def __init__(
        self,
        config: PredictiveControllerConfig,
        capacity_gpus: float,
        start_time_seconds: float,
        *,
        evidence_start_seconds: float | None = None,
        evidence_end_seconds: float | None = None,
    ) -> None:
        config.validate()
        if capacity_gpus <= 0:
            raise ValueError("capacity_gpus must be positive")
        self.config = config
        self.capacity_gpus = float(capacity_gpus)
        self.eta = config.initial_eta
        if (
            evidence_start_seconds is not None
            and evidence_end_seconds is not None
            and evidence_end_seconds < evidence_start_seconds
        ):
            raise ValueError("evidence_end_seconds cannot precede evidence_start_seconds")
        self.evidence_start_seconds = evidence_start_seconds
        self.evidence_end_seconds = evidence_end_seconds
        self.next_update_time = float(start_time_seconds)
        self._next_retrain_time = float(start_time_seconds)
        self._samples: list[tuple[float, float]] = []
        self._hourly_buckets: dict[float, float] = {}
        self._sample_by_time: dict[float, float] = {}
        self._closed_spot_runs: list[tuple[float, bool]] = []
        self._feedback_version = 0
        self._last_feedback_version = 0
        self._spot_backlog_since: float | None = None
        self._estimator = _SeasonalGaussianEstimator(config, capacity_gpus)
        self._decisions: list[dict[str, Any]] = []
        self._forecast_targets: list[tuple[float, float, float]] = []
        self._total_update_count = 0
        self._latest_quotas: dict[int, int] = {
            horizon: 0 for horizon in config.guarantee_horizons_hours
        }

    def is_update_due(self, now: float) -> bool:
        return now + EPSILON >= self.next_update_time

    def bind_evidence_window(self, start_seconds: float, end_seconds: float) -> None:
        """Bind retained decisions to the simulator's audited population window."""

        if end_seconds < start_seconds:
            raise ValueError("evidence end cannot precede evidence start")
        if self.evidence_start_seconds is None and self.evidence_end_seconds is None:
            self.evidence_start_seconds = float(start_seconds)
            self.evidence_end_seconds = float(end_seconds)
            return
        if self.evidence_start_seconds is None or self.evidence_end_seconds is None:
            raise ValueError("predictive evidence boundaries must be both set or both omitted")
        if (
            abs(self.evidence_start_seconds - start_seconds) > EPSILON
            or abs(self.evidence_end_seconds - end_seconds) > EPSILON
        ):
            raise ValueError("predictive evidence window must match the simulator window")

    def _hourly_history(self, cutoff_seconds: float) -> list[tuple[float, float]]:
        lower = cutoff_seconds - self.config.lookback_hours * 3600
        stale = [
            timestamp
            for timestamp in self._hourly_buckets
            if timestamp + EPSILON < lower
        ]
        for timestamp in stale:
            del self._hourly_buckets[timestamp]
        history = sorted(
            (timestamp, value)
            for timestamp, value in self._hourly_buckets.items()
            if timestamp <= cutoff_seconds + EPSILON
        )
        return history[-self.config.lookback_hours :]

    def _update_feedback(self, now: float, spot_backlog_gpus: float) -> dict[str, Any]:
        if spot_backlog_gpus > 0:
            if self._spot_backlog_since is None:
                self._spot_backlog_since = now
        else:
            self._spot_backlog_since = None
        lower = now - self.config.feedback_window_seconds
        self._closed_spot_runs = [
            event for event in self._closed_spot_runs if event[0] + EPSILON >= lower
        ]
        event_count = len(self._closed_spot_runs)
        evictions = sum(evicted for _timestamp, evicted in self._closed_spot_runs)
        rate = evictions / event_count if event_count else None
        previous_eta = self.eta
        reason = "no-new-completed-runs"
        target = 1.0 - self.config.guarantee_probability
        if self._feedback_version != self._last_feedback_version and rate is not None:
            if rate >= self.config.high_eviction_ratio * target and rate > 0:
                self.eta *= target / rate
                reason = "high-eviction-decrease"
            elif (
                rate <= self.config.low_eviction_ratio * target
                and spot_backlog_gpus > 0
            ):
                self.eta *= 1.0 + self.config.eta_increase_step
                reason = "low-eviction-backlog-increase"
            else:
                reason = "inside-feedback-band"
            self.eta = min(self.config.maximum_eta, max(self.config.minimum_eta, self.eta))
            self._last_feedback_version = self._feedback_version
        elif (
            self._spot_backlog_since is not None
            and now - self._spot_backlog_since
            >= self.config.starvation_increase_after_seconds
            and self.eta < self.config.maximum_eta
        ):
            self.eta *= 1.0 + self.config.eta_increase_step
            self.eta = min(self.config.maximum_eta, max(self.config.minimum_eta, self.eta))
            reason = "starvation-backlog-increase"
        return {
            "window_seconds": self.config.feedback_window_seconds,
            "completed_spot_run_count": event_count,
            "evicted_spot_run_count": evictions,
            "eviction_rate_per_run": round(rate, 9) if rate is not None else None,
            "target_eviction_rate": round(target, 9),
            "previous_eta": round(previous_eta, 9),
            "eta": round(self.eta, 9),
            "adjustment_reason": reason,
        }

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
    ) -> dict[str, Any]:
        if not self.is_update_due(now):
            raise ValueError("Controller update was requested before next_update_time")
        if self._samples and now <= self._samples[-1][0] + EPSILON:
            raise ValueError("Controller observations must advance monotonically")
        observed = max(0.0, float(hp_outstanding_requested_gpus))
        self._total_update_count += 1
        self._samples.append((float(now), observed))
        self._sample_by_time[round(float(now), 9)] = observed
        bucket = float(
            math.floor(now / self.config.aggregation_interval_seconds + EPSILON)
            * self.config.aggregation_interval_seconds
        )
        self._hourly_buckets[bucket] = max(
            observed, self._hourly_buckets.get(bucket, 0.0)
        )
        history = self._hourly_history(now)
        snapshot: dict[str, Any] = {
            "schema_version": SNAPSHOT_SCHEMA,
            "controller_id": self.config.controller_id,
            "cutoff_time_seconds": float(now),
            "history_window_seconds": {
                "start": history[0][0] if history else float(now),
                "end": float(now),
            },
            "aggregation_interval_seconds": self.config.aggregation_interval_seconds,
            "minimum_history_hours": self.config.minimum_history_hours,
            "history_ready": len(history) >= self.config.minimum_history_hours,
            "hourly_max_hp_requested_gpus": [
                {"time_seconds": timestamp, "gpus": round(value, 6)}
                for timestamp, value in history
            ],
            "current_state": {
                "hp_outstanding_requested_gpus": round(observed, 6),
                "spot_backlog_gpus": round(max(0.0, spot_backlog_gpus), 6),
                "running_spot_gpus": round(max(0.0, running_spot_gpus), 6),
            },
            "information_boundary": {
                "maximum_observed_time_seconds": float(now),
                "future_workload_fields_accepted": False,
                "source": "scheduler-observed state only",
            },
        }
        snapshot["snapshot_fingerprint"] = canonical_sha256(snapshot)
        if now + EPSILON >= self._next_retrain_time:
            self._estimator.fit(history, now)
            self._next_retrain_time = now + self.config.retrain_interval_seconds
        points, model = self._estimator.forecast(history, now)
        forecast: dict[str, Any] = {
            "schema_version": FORECAST_SCHEMA,
            "controller_id": self.config.controller_id,
            "controller_fingerprint": self.config.fingerprint,
            "observation_snapshot_fingerprint": snapshot["snapshot_fingerprint"],
            "cutoff_time_seconds": float(now),
            "forecast_horizon_hours": self.config.forecast_horizon_hours,
            "guarantee_probability": self.config.guarantee_probability,
            "model": model,
            "points": points,
            "information_boundary": {
                "inputs_end_at_cutoff": True,
                "actual_future_demand_used_for_prediction": False,
            },
        }
        forecast["forecast_fingerprint"] = canonical_sha256(forecast)
        inside_evidence = (
            (self.evidence_start_seconds is None or now + EPSILON >= self.evidence_start_seconds)
            and (self.evidence_end_seconds is None or now <= self.evidence_end_seconds + EPSILON)
        )
        if inside_evidence:
            self._forecast_targets.extend(
                (
                    float(point["target_time_seconds"]),
                    float(point["mu_gpus"]),
                    float(point["guarantee_quantile_gpus"]),
                )
                for point in points
                if self.evidence_end_seconds is None
                or float(point["target_time_seconds"])
                <= self.evidence_end_seconds + EPSILON
            )
        feedback = self._update_feedback(now, spot_backlog_gpus)
        free_by_step = [
            max(0.0, self.capacity_gpus - point["guarantee_quantile_gpus"])
            for point in points
        ]
        quotas: dict[str, int] = {}
        for horizon in self.config.guarantee_horizons_hours:
            free = min(free_by_step[:horizon])
            quota = int(
                max(0.0, min(self.capacity_gpus, math.floor(free * self.eta + EPSILON)))
            )
            self._latest_quotas[horizon] = quota
            quotas[str(horizon)] = quota
        quota_plan: dict[str, Any] = {
            "schema_version": QUOTA_SCHEMA,
            "controller_id": self.config.controller_id,
            "controller_fingerprint": self.config.fingerprint,
            "forecast_fingerprint": forecast["forecast_fingerprint"],
            "cutoff_time_seconds": float(now),
            "capacity_gpus": self.capacity_gpus,
            "eta": round(self.eta, 9),
            "guarantee_probability": self.config.guarantee_probability,
            "spot_quota_gpus_by_guarantee_hour": quotas,
            "rule": "eta * minimum forecast free capacity across the guarantee horizon",
        }
        quota_plan["quota_plan_fingerprint"] = canonical_sha256(quota_plan)
        decision: dict[str, Any] = {
            "schema_version": DECISION_SCHEMA,
            "cutoff_time_seconds": float(now),
            "snapshot": snapshot,
            "forecast": forecast,
            "feedback": feedback,
            "quota_plan": quota_plan,
        }
        decision["decision_fingerprint"] = canonical_sha256(decision)
        compact_decision: dict[str, Any] = {
            "schema_version": DECISION_SCHEMA,
            "cutoff_time_seconds": float(now),
            "snapshot_fingerprint": snapshot["snapshot_fingerprint"],
            "forecast_fingerprint": forecast["forecast_fingerprint"],
            "quota_plan_fingerprint": quota_plan["quota_plan_fingerprint"],
            "model_fingerprint": forecast["model"]["model_fingerprint"],
            "trained_at_seconds": forecast["model"]["trained_at_seconds"],
            "history_ready": snapshot["history_ready"],
            "hp_outstanding_requested_gpus": snapshot["current_state"][
                "hp_outstanding_requested_gpus"
            ],
            "spot_backlog_gpus": snapshot["current_state"]["spot_backlog_gpus"],
            "running_spot_gpus": snapshot["current_state"]["running_spot_gpus"],
            "eta": quota_plan["eta"],
            "spot_quota_gpus_by_guarantee_hour": quota_plan[
                "spot_quota_gpus_by_guarantee_hour"
            ],
            "feedback_eviction_rate_per_run": feedback["eviction_rate_per_run"],
            "feedback_adjustment_reason": feedback["adjustment_reason"],
        }
        compact_decision["decision_fingerprint"] = canonical_sha256(compact_decision)
        if inside_evidence:
            self._decisions.append(compact_decision)
        self.next_update_time = now + self.config.observation_interval_seconds
        return decision

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
        self._closed_spot_runs.append((float(now), bool(evicted)))
        self._feedback_version += 1

    def quota_for_guarantee_seconds(
        self, guarantee_seconds: int, resource_pool: str | None = None
    ) -> int:
        required_hours = max(1, int(math.ceil(guarantee_seconds / 3600)))
        for horizon in self.config.guarantee_horizons_hours:
            if horizon >= required_hours:
                return self._latest_quotas[horizon]
        raise ValueError(
            "Policy guarantee exceeds the predictive controller's declared horizons"
        )

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

    def finalize(self) -> dict[str, Any]:
        errors: list[float] = []
        absolute_errors: list[float] = []
        actuals: list[float] = []
        covered = 0
        pinball_losses: list[float] = []
        q = self.config.guarantee_probability
        for target_time, mu, guarantee_quantile in self._forecast_targets:
            target = round(target_time, 9)
            if target not in self._sample_by_time:
                continue
            actual = self._sample_by_time[target]
            error = actual - mu
            quantile_error = actual - guarantee_quantile
            errors.append(error)
            absolute_errors.append(abs(error))
            actuals.append(actual)
            covered += actual <= guarantee_quantile + EPSILON
            pinball_losses.append(
                max(q * quantile_error, (q - 1.0) * quantile_error)
            )
        quotas = [
            quota
            for decision in self._decisions
            for quota in decision["spot_quota_gpus_by_guarantee_hour"].values()
        ]
        etas = [float(decision["eta"]) for decision in self._decisions]
        evaluation_count = len(actuals)
        report: dict[str, Any] = {
            "schema_version": CONTROL_REPORT_SCHEMA,
            "controller_id": self.config.controller_id,
            "controller": self.config.to_dict(),
            "controller_fingerprint": self.config.fingerprint,
            "information_boundary": {
                "future_workload_fields_accepted": False,
                "forecast_scoring_occurs_after_target_observation": True,
            },
            "update_count": len(self._decisions),
            "total_runtime_update_count": self._total_update_count,
            "evidence_window_seconds": {
                "start": self.evidence_start_seconds,
                "end": self.evidence_end_seconds,
            },
            "first_cutoff_time_seconds": (
                self._decisions[0]["cutoff_time_seconds"] if self._decisions else None
            ),
            "last_cutoff_time_seconds": (
                self._decisions[-1]["cutoff_time_seconds"] if self._decisions else None
            ),
            "eta": {
                "initial": self.config.initial_eta,
                "final": round(self.eta, 9),
                "minimum_observed": min(etas) if etas else None,
                "maximum_observed": max(etas) if etas else None,
            },
            "spot_quota_gpus": {
                "mean": round(_mean([float(value) for value in quotas]), 6)
                if quotas
                else None,
                "minimum": min(quotas) if quotas else None,
                "maximum": max(quotas) if quotas else None,
            },
            "forecast_evaluation": {
                "evaluated_point_count": evaluation_count,
                "mae_gpus": round(_mean(absolute_errors), 6)
                if absolute_errors
                else None,
                "wape": (
                    round(sum(absolute_errors) / sum(actuals), 9)
                    if actuals and sum(actuals) > 0
                    else None
                ),
                "mean_error_gpus": round(_mean(errors), 6) if errors else None,
                "guarantee_quantile_coverage": (
                    round(covered / evaluation_count, 9) if evaluation_count else None
                ),
                "pinball_loss": round(_mean(pinball_losses), 6)
                if pinball_losses
                else None,
                "definition": "Targets are scored only after the matching scheduler observation exists.",
            },
            "decisions": self._decisions,
        }
        report["control_fingerprint"] = canonical_sha256(report)
        return report


def build_observation_bundle(
    trace: CanonicalTrace,
    config: PredictiveControllerConfig,
    cutoff_seconds: float,
) -> dict[str, Any]:
    """Reconstruct scheduler-visible history up to a cutoff for Agent analysis."""

    if cutoff_seconds < min(job.submit_time_seconds for job in trace.jobs):
        raise ValueError("cutoff_seconds precedes the first trace arrival")
    earliest = max(
        min(job.submit_time_seconds for job in trace.jobs),
        cutoff_seconds - config.lookback_hours * 3600,
    )
    steps = int(
        math.floor(
            (cutoff_seconds - earliest) / config.observation_interval_seconds
            + EPSILON
        )
    )
    start = cutoff_seconds - steps * config.observation_interval_seconds
    controller = PredictiveSpotController(config, trace.capacity_gpus, start)
    events: list[tuple[float, int, str, float]] = []
    for job in trace.jobs:
        if job.submit_time_seconds > cutoff_seconds + EPSILON:
            continue
        events.append(
            (job.submit_time_seconds, 1, job.service_class, float(job.gpu_count))
        )
        completion = job.submit_time_seconds + job.duration_seconds
        if completion <= cutoff_seconds + EPSILON:
            events.append((completion, 0, job.service_class, -float(job.gpu_count)))
    events.sort()
    event_index = 0
    outstanding = {"HP": 0.0, "Spot": 0.0}
    now = start
    latest: dict[str, Any] | None = None
    while now <= cutoff_seconds + EPSILON:
        while event_index < len(events) and events[event_index][0] <= now + EPSILON:
            _timestamp, _order, service_class, delta = events[event_index]
            outstanding[service_class] += delta
            event_index += 1
        latest = controller.update(
            now,
            hp_outstanding_requested_gpus=outstanding["HP"],
            spot_backlog_gpus=outstanding["Spot"],
            running_spot_gpus=0.0,
        )
        now += config.observation_interval_seconds
    if latest is None:
        raise RuntimeError("Observation replay produced no controller decision")
    observed_jobs = [
        job for job in trace.jobs if job.submit_time_seconds <= cutoff_seconds + EPSILON
    ]
    observed_prefix = {
        "trace_id": trace.trace_id,
        "capacity_gpus": trace.capacity_gpus,
        "cutoff_time_seconds": float(cutoff_seconds),
        "jobs": [
            {
                "job_id": job.job_id,
                "submit_time_seconds": job.submit_time_seconds,
                "gpu_count": job.gpu_count,
                "gpu_model": job.gpu_model,
                "service_class": job.service_class,
                "completed_by_cutoff": (
                    job.submit_time_seconds + job.duration_seconds
                    <= cutoff_seconds + EPSILON
                ),
            }
            for job in observed_jobs
        ],
    }
    bundle: dict[str, Any] = {
        "schema_version": OBSERVATION_BUNDLE_SCHEMA,
        "trace_id": trace.trace_id,
        "observed_prefix_fingerprint": canonical_sha256(observed_prefix),
        "controller_id": config.controller_id,
        "controller_fingerprint": config.fingerprint,
        "cutoff_time_seconds": float(cutoff_seconds),
        "observation_snapshot": latest["snapshot"],
        "demand_forecast": latest["forecast"],
        "spot_quota_plan": latest["quota_plan"],
        "information_boundary": {
            "jobs_with_submit_time_after_cutoff_excluded": True,
            "full_trace_fingerprint_exposed": False,
            "actual_future_demand_used_for_prediction": False,
        },
    }
    bundle["observation_bundle_fingerprint"] = canonical_sha256(bundle)
    return bundle
