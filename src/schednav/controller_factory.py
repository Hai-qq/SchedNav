"""Loading and construction for registered predictive controller families."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeAlias

from .native_trace import CanonicalTrace
from .predictive_control import (
    CONTROLLER_SCHEMA,
    PredictiveControllerConfig,
    PredictiveSpotController,
    build_observation_bundle,
)
from .tenant_predictive_control import (
    TENANT_CONTROLLER_SCHEMA,
    TenantPredictiveControllerConfig,
    TenantPredictiveSpotController,
    build_tenant_observation_bundle,
    validate_tenant_predictive_trace,
)


ControllerConfig: TypeAlias = (
    PredictiveControllerConfig | TenantPredictiveControllerConfig
)
PredictiveController: TypeAlias = (
    PredictiveSpotController | TenantPredictiveSpotController
)


def load_controller_config(path: Path) -> ControllerConfig:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Predictive controller config must be a JSON object")
    schema = value.get("schema_version")
    if schema == CONTROLLER_SCHEMA:
        return PredictiveControllerConfig.from_dict(value)
    if schema == TENANT_CONTROLLER_SCHEMA:
        return TenantPredictiveControllerConfig.from_dict(value)
    raise ValueError(
        "Unsupported predictive controller schema; expected "
        f"{CONTROLLER_SCHEMA} or {TENANT_CONTROLLER_SCHEMA}"
    )


def create_predictive_controller(
    config: ControllerConfig,
    trace: CanonicalTrace,
    start_time_seconds: float,
    *,
    evidence_start_seconds: float,
    evidence_end_seconds: float,
) -> PredictiveController:
    if isinstance(config, TenantPredictiveControllerConfig):
        validate_tenant_predictive_trace(trace)
        capacities: dict[str, float] = {}
        for node in trace.nodes:
            capacities[node.gpu_model] = capacities.get(node.gpu_model, 0.0) + node.gpu_count
        return TenantPredictiveSpotController(
            config,
            capacities,
            start_time_seconds,
            trace.time_origin,
            evidence_start_seconds=evidence_start_seconds,
            evidence_end_seconds=evidence_end_seconds,
        )
    return PredictiveSpotController(
        config,
        trace.capacity_gpus,
        start_time_seconds,
        evidence_start_seconds=evidence_start_seconds,
        evidence_end_seconds=evidence_end_seconds,
    )


def build_controller_observation_bundle(
    trace: CanonicalTrace,
    config: ControllerConfig,
    cutoff_seconds: float,
) -> dict:
    if isinstance(config, TenantPredictiveControllerConfig):
        return build_tenant_observation_bundle(trace, config, cutoff_seconds)
    return build_observation_bundle(trace, config, cutoff_seconds)
