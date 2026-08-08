"""Finite high-level policy action validation and GFS run-spec materialization."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from .contracts import RunSpec, canonical_sha256


ACTION_FIELDS = {
    "schema_version",
    "action_id",
    "scheduler",
    "guarantee_hours",
    "guarantee_rate",
    "ckpt_interval_seconds",
}


def validate_policy_action(action: dict[str, Any], action_space: dict[str, Any]) -> dict[str, Any]:
    if action.get("schema_version") != "schednav.policy-action/v1":
        raise ValueError("Unsupported policy action schema")
    if set(action) != ACTION_FIELDS:
        raise ValueError(f"Policy action fields must be exactly {sorted(ACTION_FIELDS)}")
    if action_space.get("schema_version") != "schednav.action-space/v1":
        raise ValueError("Unsupported action-space schema")
    allowed = action_space["allowed"]
    normalized = {
        "schema_version": action["schema_version"],
        "action_id": str(action["action_id"]),
        "scheduler": str(action["scheduler"]),
        "guarantee_hours": [int(value) for value in action["guarantee_hours"]],
        "guarantee_rate": float(action["guarantee_rate"]),
        "ckpt_interval_seconds": int(action["ckpt_interval_seconds"]),
    }
    checks = {
        "scheduler": normalized["scheduler"] in allowed["scheduler"],
        "guarantee_hours": normalized["guarantee_hours"] in allowed["guarantee_hours"],
        "guarantee_rate": normalized["guarantee_rate"] in allowed["guarantee_rate"],
        "ckpt_interval_seconds": normalized["ckpt_interval_seconds"] in allowed["ckpt_interval_seconds"],
        "curated_profile": normalized in action_space.get("profiles", []),
    }
    rejected = [name for name, accepted in checks.items() if not accepted]
    if rejected:
        raise ValueError(f"Policy action is outside the finite action space: {rejected}")
    return normalized


def materialize_policy_action(
    base_config_path: Path,
    action_space_path: Path,
    action_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    base = json.loads(base_config_path.read_text(encoding="utf-8"))
    action_space = json.loads(action_space_path.read_text(encoding="utf-8"))
    action = validate_policy_action(json.loads(action_path.read_text(encoding="utf-8")), action_space)
    run_spec = deepcopy(base)
    run_spec["experiment_name"] = action["action_id"]
    for field in ("scheduler", "guarantee_hours", "guarantee_rate", "ckpt_interval_seconds"):
        run_spec["policy"][field] = action[field]
    validated = RunSpec.from_dict(run_spec)
    receipt = {
        "schema_version": "schednav.policy-materialization/v1",
        "action_space_fingerprint": canonical_sha256(action_space),
        "action_fingerprint": canonical_sha256(action),
        "run_spec_fingerprint": validated.fingerprint,
        "controlled_fields": [
            "experiment_name",
            "policy.scheduler",
            "policy.guarantee_hours",
            "policy.guarantee_rate",
            "policy.ckpt_interval_seconds",
        ],
    }
    receipt["materialization_fingerprint"] = canonical_sha256(receipt)
    return run_spec, receipt
