"""Collect completed, fingerprint-bound rolling plans from the local bridge store."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from schednav.contracts import canonical_sha256


PLAN_SCHEMA = "schednav.rolling-agent-plan/v1"
MANIFEST_SCHEMA = "schednav.rolling-agent-plan-set/v1"
STAGE_SCHEMA = "schednav.agent-stage-output/v1"
ISOLATION_SCHEMAS = {
    "schednav.agentteams-context-isolation/v1",
    "schednav.agentteams-context-isolation/v2",
}


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _load_task_meta(path: Path) -> tuple[dict[str, Any], bool]:
    """Read AgentTeams metadata while preserving a known literal-newline anomaly."""

    raw = path.read_text(encoding="utf-8")
    try:
        value = json.loads(raw)
        normalized = False
    except json.JSONDecodeError:
        if not raw.endswith("\\n"):
            raise
        value = json.loads(raw[:-2])
        normalized = True
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value, normalized


def _verified(value: dict[str, Any], field: str) -> bool:
    supplied = value.get(field)
    payload = {key: item for key, item in value.items() if key != field}
    return isinstance(supplied, str) and canonical_sha256(payload) == supplied


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Missing AgentTeams isolation timestamp: {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Invalid AgentTeams isolation timestamp: {field}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"AgentTeams isolation timestamp is not timezone-aware: {field}")
    return parsed


def _verify_context_isolation(
    task_directory: Path,
    *,
    meta: dict[str, Any],
    project_id: str,
    task_id: str,
    worker_id: str,
    observation_fingerprint: str,
) -> None:
    receipt_path = task_directory / "context-isolation.json"
    if not receipt_path.is_file():
        raise ValueError(f"Missing AgentTeams context-isolation receipt: {task_id}")
    isolation = _load(receipt_path)
    isolation_schema = isolation.get("schema_version")
    if isolation_schema not in ISOLATION_SCHEMAS or not _verified(
        isolation, "receipt_fingerprint"
    ):
        raise ValueError(f"Invalid AgentTeams context-isolation receipt: {task_id}")
    private_room_id = isolation.get("worker_private_room_id")
    project_room_id = isolation.get("project_room_id")
    if (
        isolation.get("project_id") != project_id
        or isolation.get("task_id") != task_id
        or isolation.get("worker_id") != worker_id
        or isolation.get("scope") != "one-controller-one-observation"
        or isolation.get("observation_fingerprint") != observation_fingerprint
        or isolation.get("assignment_context_verified") is not True
        or isolation.get("cross_window_context_visible") is not False
        or private_room_id != meta.get("room_id")
        or not isinstance(project_room_id, str)
        or private_room_id == project_room_id
    ):
        raise ValueError(f"AgentTeams task lacks verified context isolation: {task_id}")
    if isolation_schema == "schednav.agentteams-context-isolation/v1":
        if (
            isolation.get("clear_command") != "/clear"
            or isolation.get("clear_acknowledged") is not True
            or isolation.get("assignment_context_evidence")
            != "worker-log-handle-agent-query"
        ):
            raise ValueError(
                f"AgentTeams task lacks verified cleared-room isolation: {task_id}"
            )
        isolation_ready_at = _utc_timestamp(
            isolation.get("clear_acknowledged_at"), field="clear_acknowledged_at"
        )
        ordering_error = "assigned before context clear"
    else:
        if (
            isolation.get("isolation_method")
            != "fresh-private-room-single-assignment"
            or isolation.get("fresh_room_created_for_task") is not True
            or isolation.get("assignment_context_evidence")
            != "fresh-private-room-single-assignment"
        ):
            raise ValueError(
                f"AgentTeams task lacks verified fresh-room isolation: {task_id}"
            )
        isolation_ready_at = _utc_timestamp(
            isolation.get("fresh_room_verified_at"), field="fresh_room_verified_at"
        )
        ordering_error = "assigned before fresh-room verification"
    dispatched_at = _utc_timestamp(
        isolation.get("assignment_dispatched_at"), field="assignment_dispatched_at"
    )
    if dispatched_at < isolation_ready_at:
        raise ValueError(f"AgentTeams task was {ordering_error}: {task_id}")


def _verify_stage_receipt(
    receipt: dict[str, Any],
    *,
    project_id: str,
    observation_fingerprint: str,
    candidate_action_ids: list[str],
    reason_code: str,
    task_root: Path,
    source_anomalies: set[str] | None = None,
) -> Path:
    task_id = str(receipt.get("task_id", ""))
    task_directory = (task_root / task_id).resolve()
    if task_directory.parent != task_root.resolve() or not task_directory.is_dir():
        raise ValueError(f"Missing AgentTeams task directory: {task_id}")
    meta, normalized_meta = _load_task_meta(task_directory / "meta.json")
    if normalized_meta and source_anomalies is not None:
        source_anomalies.add(task_id)
    if (
        meta.get("task_id") != task_id
        or meta.get("project_id") != project_id
        or meta.get("assigned_to") != receipt.get("worker_id")
        or meta.get("status") not in {"submitted", "completed"}
    ):
        raise ValueError(f"AgentTeams task identity mismatch: {task_id}")
    _verify_context_isolation(
        task_directory,
        meta=meta,
        project_id=project_id,
        task_id=task_id,
        worker_id=str(receipt.get("worker_id", "")),
        observation_fingerprint=observation_fingerprint,
    )
    workspace = task_directory / "workspace"
    candidates = sorted(workspace.glob("normalized-stage-*.json"))
    matches: list[Path] = []
    for path in candidates:
        value = _load(path)
        if (
            value.get("schema_version") == STAGE_SCHEMA
            and value.get("observation_fingerprint") == observation_fingerprint
            and value.get("role") == receipt.get("role")
            and value.get("worker_id") == receipt.get("worker_id")
            and value.get("task_id") == task_id
            and value.get("model_id") == "deepseek-v4-flash"
        ):
            matches.append(path)
    if len(matches) != 1:
        raise ValueError(
            f"Expected one matching normalized stage output for {task_id}, found {len(matches)}"
        )
    path = matches[0]
    if _file_sha256(path) != receipt.get("output_fingerprint"):
        raise ValueError(f"AgentTeams stage fingerprint mismatch: {path}")
    value = _load(path)
    if receipt.get("role") == "Scheduling Strategist" and (
        value.get("candidate_action_ids") != candidate_action_ids
        or value.get("reason_code") != reason_code
    ):
        raise ValueError(f"Strategist stage output differs from frozen decision: {path}")
    return path


def _validate_plan(
    value: dict[str, Any],
    *,
    project_id: str,
    mode: str,
    controller_id: str,
    task_root: Path,
    source_anomalies: set[str],
    required_action_id: str = "native-fifo",
) -> int:
    if value.get("schema_version") != PLAN_SCHEMA or not _verified(
        value, "plan_fingerprint"
    ):
        raise ValueError(f"Invalid rolling Agent plan: {controller_id}")
    if value.get("controller_id") != controller_id or value.get("mode") != mode:
        raise ValueError(f"Rolling plan identity mismatch: {controller_id}")
    if value.get("model_id") != "deepseek-v4-flash":
        raise ValueError("Rolling plan used an unapproved model")
    declared_action_id = str(
        value.get("required_candidate_action_id", "native-fifo")
    )
    if declared_action_id != required_action_id:
        raise ValueError("Rolling plan belongs to another safety baseline")
    if value.get("source", {}).get("framework") != "AgentTeams" or value.get(
        "source", {}
    ).get("project_id") != project_id:
        raise ValueError("Rolling plan has the wrong AgentTeams provenance")
    expected_roles = (
        {"Scheduling Strategist"}
        if mode == "single_agent"
        else {"Workload Analyst", "Scheduling Strategist"}
    )
    observations: set[str] = set()
    verified_receipts: set[tuple[str, str]] = set()
    for decision in value.get("decisions", []):
        observation = decision.get("observation_fingerprint")
        if not isinstance(observation, str) or observation in observations:
            raise ValueError("Rolling plan observations must be unique")
        observations.add(observation)
        actions = decision.get("candidate_action_ids")
        if (
            not isinstance(actions, list)
            or len(actions) != 3
            or len(set(actions)) != 3
            or required_action_id not in actions
        ):
            raise ValueError("Every Agent decision must consume the frozen budget")
        receipts = decision.get("agent_stage_receipts", [])
        roles = {receipt.get("role") for receipt in receipts}
        if not expected_roles.issubset(roles):
            raise ValueError("Agent plan is missing a required role receipt")
        for receipt in receipts:
            stage = _verify_stage_receipt(
                receipt,
                project_id=project_id,
                observation_fingerprint=observation,
                candidate_action_ids=actions,
                reason_code=str(decision.get("reason_code", "")),
                task_root=task_root,
                source_anomalies=source_anomalies,
            )
            if (
                mode == "multi_agent_masked"
                and receipt.get("role") == "Workload Analyst"
                and _load(stage).get("analysis")
                != {
                    "masked": True,
                    "content": "withheld-for-causal-ablation",
                }
            ):
                raise ValueError(
                    "Masked-handoff Analyst stage contains workload information"
                )
            identity = (str(receipt.get("task_id")), _file_sha256(stage))
            if identity in verified_receipts:
                raise ValueError("Agent stage receipt is reused across rolling decisions")
            verified_receipts.add(identity)
        if int(decision.get("llm_call_count", 0)) < len(expected_roles):
            raise ValueError("Agent plan under-reports its LLM stages")
    if not observations:
        raise ValueError("Rolling plan contains no decisions")
    return len(verified_receipts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--study", default="configs/studies/rolling-ablation-v1.json")
    parser.add_argument(
        "--bridge-task-root",
        action="append",
        dest="bridge_task_roots",
        help=(
            "Bridge tasks directory; repeat for parallel deterministic lanes. "
            "Defaults to artifacts/agentteams-rolling-bridge-v1/tasks."
        ),
    )
    parser.add_argument(
        "--agentteams-task-root",
        required=True,
        help="Exported AgentTeams shared/tasks directory used to verify stage hashes",
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    study = _load((root / args.study).resolve())
    action_space = _load(
        (root / study["rolling_contract"]["action_space"]).resolve()
    )
    required_action_id = str(
        action_space.get("safety_baseline_action_id", "native-fifo")
    )
    bridge_roots = [
        (root / value).resolve()
        for value in (
            args.bridge_task_roots
            or ["artifacts/agentteams-rolling-bridge-v1/tasks"]
        )
    ]
    missing_bridge_roots = [path for path in bridge_roots if not path.is_dir()]
    if missing_bridge_roots:
        raise FileNotFoundError(f"Missing bridge task roots: {missing_bridge_roots}")
    agentteams_task_root = (root / args.agentteams_task_root).resolve()
    if not agentteams_task_root.is_dir():
        raise FileNotFoundError(
            f"Missing exported AgentTeams task root: {agentteams_task_root}"
        )
    output_dir = (root / args.output_dir).resolve()
    discovered: dict[str, tuple[Path, dict[str, Any]]] = {}
    for bridge_root in bridge_roots:
        for path in sorted(bridge_root.glob("*/rolling-agent-plan.json")):
            value = _load(path)
            if value.get("source", {}).get("project_id") != args.project_id:
                continue
            controller_id = str(value.get("controller_id", ""))
            if controller_id in discovered:
                raise ValueError(f"Duplicate completed plan: {controller_id}")
            discovered[controller_id] = (path, value)

    entries = []
    verified_stage_receipt_count = 0
    source_anomalies: set[str] = set()
    controller_prefix = (
        "schednav-rollab"
        if study["study_id"] == "rolling-ablation-v1"
        else f"schednav-{study['study_id']}"
    )
    modes = [
        str(item["mode"])
        for item in study["arms"]
        if item.get("mode")
        in {"single_agent", "multi_agent", "multi_agent_masked"}
    ]
    for window in study["holdout_windows"]:
        window_id = str(window["window_id"])
        for mode in modes:
            controller_id = f"{controller_prefix}-{mode}-{window_id}"
            if controller_id not in discovered:
                raise FileNotFoundError(f"Missing completed AgentTeams plan: {controller_id}")
            source_path, plan = discovered[controller_id]
            verified_stage_receipt_count += _validate_plan(
                plan,
                project_id=args.project_id,
                mode=mode,
                controller_id=controller_id,
                task_root=agentteams_task_root,
                source_anomalies=source_anomalies,
                required_action_id=required_action_id,
            )
            destination = output_dir / window_id / f"{mode}.json"
            _write(destination, plan)
            entries.append(
                {
                    "window_id": window_id,
                    "mode": mode,
                    "controller_id": controller_id,
                    "plan_fingerprint": plan["plan_fingerprint"],
                    "decision_count": len(plan["decisions"]),
                    "source_task_directory": source_path.parent.name,
                    "artifact": destination.relative_to(root).as_posix(),
                }
            )

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "project_id": args.project_id,
        "framework": "AgentTeams",
        "model_id": "deepseek-v4-flash",
        "design_fingerprint": study["design_fingerprint"],
        "stage_receipt_verification": {
            "status": "verified",
            "receipt_count": verified_stage_receipt_count,
            "context_isolation": "verified",
        },
        "plans": entries,
    }
    if source_anomalies:
        manifest["source_anomalies"] = {
            "task_meta_trailing_literal_newline": sorted(source_anomalies),
            "handling": "read-only parse normalization; source bytes unmodified",
        }
    manifest["manifest_fingerprint"] = canonical_sha256(manifest)
    _write(output_dir / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
