"""Validate and content-address a raw AgentTeams controller artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT_DEFAULT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from schednav.adaptive_benchmark import (
    CONTROLLER_SCHEMA,
    validate_controller_selections,
)
from schednav.contracts import canonical_sha256


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--design", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--artifact-ref", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    raw = _load(Path(args.raw).resolve())
    design = _load(Path(args.design).resolve())
    allowed = {
        "schema_version",
        "controller_id",
        "design_fingerprint",
        "selection_basis",
        "model_id",
        "windows",
        "definition",
    }
    if not set(raw).issubset(allowed):
        raise ValueError(f"Raw AgentTeams artifact has unsupported fields: {sorted(set(raw) - allowed)}")
    if raw.get("design_fingerprint") != design.get("design_fingerprint"):
        raise ValueError("Raw AgentTeams artifact has the wrong design fingerprint")
    if raw.get("model_id") != "deepseek-v4-flash":
        raise ValueError("Raw AgentTeams artifact has the wrong model_id")
    windows = raw.get("windows")
    if not isinstance(windows, list):
        raise ValueError("Raw AgentTeams artifact must contain a windows list")
    for item in windows:
        if not isinstance(item, dict) or set(item) != {
            "window_id",
            "candidate_action_ids",
            "reason_code",
        }:
            raise ValueError("Every raw AgentTeams window must use the exact controller fields")
    controller = {
        "schema_version": CONTROLLER_SCHEMA,
        "controller_id": "agentteams-deepseek-v4-flash-v1",
        "design_fingerprint": design["design_fingerprint"],
        "selection_basis": "workload_only",
        "model_id": "deepseek-v4-flash",
        "windows": windows,
        "definition": (
            "AgentTeams Workload Analyst and Scheduling Strategist selected bounded "
            "candidate sets from the frozen workload-only design before v3 simulation."
        ),
        "provenance": {
            "agentteams_project_id": args.project_id,
            "agentteams_task_id": args.task_id,
            "raw_artifact_ref": args.artifact_ref,
        },
    }
    controller["controller_fingerprint"] = canonical_sha256(controller)
    validate_controller_selections(controller, design)
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(controller, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "window_count": len(windows),
                "controller_fingerprint": controller["controller_fingerprint"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
