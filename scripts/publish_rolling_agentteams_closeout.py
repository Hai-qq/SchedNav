"""Verify AgentTeams closeout stages and publish a compact rolling receipt."""

from __future__ import annotations

import argparse
from functools import cmp_to_key
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT_DEFAULT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from schednav.contracts import canonical_sha256


MODEL_ID = "deepseek-v4-flash"
PROJECT_ID_DEFAULT = "proj-20260810-062224"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _verified(value: dict[str, Any], field: str) -> bool:
    supplied = value.get(field)
    payload = {key: item for key, item in value.items() if key != field}
    return isinstance(supplied, str) and canonical_sha256(payload) == supplied


def _byte_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_meta(
    task_dir: Path,
    task_id: str,
    worker_id: str,
    project_id: str,
) -> None:
    meta = _load(task_dir / "meta.json")
    if (
        meta.get("task_id") != task_id
        or meta.get("project_id") != project_id
        or meta.get("assigned_to") != worker_id
        or meta.get("status") not in {"submitted", "completed"}
    ):
        raise ValueError(f"AgentTeams task metadata is invalid: {task_id}")


def _validate_isolation(
    task_dir: Path,
    *,
    task_id: str,
    worker_id: str,
    evidence_fingerprint: str,
    project_id: str,
) -> str | None:
    path = task_dir / "context-isolation.json"
    if not path.exists():
        return None
    receipt = _load(path)
    schema_version = receipt.get("schema_version")
    protocol_valid = (
        schema_version == "schednav.agentteams-context-isolation/v1"
        and receipt.get("clear_acknowledged") is True
        and receipt.get("assignment_context_evidence")
        == "worker-log-handle-agent-query"
    ) or (
        schema_version == "schednav.agentteams-context-isolation/v2"
        and receipt.get("isolation_method")
        == "fresh-private-room-single-assignment"
        and receipt.get("fresh_room_created_for_task") is True
        and receipt.get("assignment_context_evidence")
        == "fresh-private-room-single-assignment"
    )
    if (
        not _verified(receipt, "receipt_fingerprint")
        or receipt.get("project_id") != project_id
        or receipt.get("task_id") != task_id
        or receipt.get("worker_id") != worker_id
        or receipt.get("observation_fingerprint") != evidence_fingerprint
        or not protocol_valid
        or receipt.get("assignment_context_verified") is not True
        or receipt.get("cross_window_context_visible") is not False
    ):
        raise ValueError(f"AgentTeams context-isolation receipt is invalid: {task_id}")
    return str(receipt["receipt_fingerprint"])


def _validate_stage(
    task_dir: Path,
    *,
    task_id: str,
    role: str,
    worker_id: str,
    normalized_name: str,
    wrapper_name: str,
    payload_key: str,
    evidence_fingerprint: str,
    expected_receipts: int,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    normalized_path = task_dir / "workspace" / normalized_name
    wrapper_path = task_dir / "workspace" / wrapper_name
    normalized = _load(normalized_path)
    wrapper = _load(wrapper_path)
    identity = {
        "schema_version": "schednav.agent-stage-output/v1",
        "observation_fingerprint": evidence_fingerprint,
        "model_id": MODEL_ID,
        "role": role,
        "worker_id": worker_id,
        "task_id": task_id,
    }
    for key, expected in identity.items():
        if normalized.get(key) != expected or wrapper.get(key) != expected:
            raise ValueError(f"Invalid {role} identity field {key}: {task_id}")
    if normalized.get(payload_key) != wrapper.get(payload_key):
        raise ValueError(f"{role} wrapper changed the normalized payload")
    digest = _byte_sha256(normalized_path)
    receipts = wrapper.get("agent_stage_receipts")
    if not isinstance(receipts, list) or len(receipts) != expected_receipts:
        raise ValueError(f"Unexpected {role} receipt count")
    own = [item for item in receipts if item.get("task_id") == task_id]
    if own != [
        {
            "role": role,
            "worker_id": worker_id,
            "task_id": task_id,
            "output_fingerprint": digest,
        }
    ]:
        raise ValueError(f"{role} byte receipt does not match its normalized output")
    if (
        wrapper.get("llm_call_count") != 1
        or wrapper.get("prompt_tokens") != 0
        or wrapper.get("completion_tokens") != 0
        or wrapper.get("token_count_status") != "unavailable"
    ):
        raise ValueError(f"{role} LLM accounting is invalid")
    return normalized, wrapper, digest


def _expected_eligible(evidence: dict[str, Any]) -> list[str]:
    window_count = int(evidence["window_count"])
    return sorted(
        arm_id
        for arm_id, aggregate in evidence["arms"].items()
        if arm_id != "posthoc-catalog-oracle"
        and int(aggregate["hard_slo_pass_count"]) == window_count
    )


def _expected_verification_counts(evidence: dict[str, Any]) -> dict[str, int]:
    """Derive closeout counts from the compact evidence instead of a study version."""

    window_count = int(evidence["window_count"])
    rolling_arm_count = sum(
        1
        for arm_id, aggregate in evidence["arms"].items()
        if arm_id != "posthoc-catalog-oracle"
        and int(aggregate.get("candidate_simulation_count", 0)) > 0
    )
    return {
        "record": int(evidence["record_count"]),
        "deterministic_repetition": int(evidence["record_count"]),
        "rolling_boundary": window_count * rolling_arm_count,
    }


def _rank(evidence: dict[str, Any], eligible: list[str]) -> str | None:
    if not eligible:
        return None

    def compare(left_id: str, right_id: str) -> int:
        left = evidence["arms"][left_id]["mean_metrics"]
        right = evidence["arms"][right_id]["mean_metrics"]
        allocation_delta = left["allocation_rate_mean"] - right["allocation_rate_mean"]
        if abs(allocation_delta) >= 0.01:
            return -1 if allocation_delta > 0 else 1
        spot_delta = left["spot_jct_p95_seconds"] - right["spot_jct_p95_seconds"]
        if spot_delta:
            return -1 if spot_delta < 0 else 1
        eviction_delta = (
            left["spot_eviction_rate_per_run"]
            - right["spot_eviction_rate_per_run"]
        )
        if eviction_delta:
            return -1 if eviction_delta < 0 else 1
        return 0

    ordered = sorted(eligible, key=cmp_to_key(compare))
    if len(ordered) > 1 and compare(ordered[0], ordered[1]) == 0:
        return None
    return ordered[0]


def build_receipt(
    *,
    evidence_path: Path,
    task_root: Path,
    audit_task_id: str,
    manager_task_id: str,
    project_id: str = PROJECT_ID_DEFAULT,
) -> dict[str, Any]:
    evidence = _load(evidence_path)
    if (
        evidence.get("schema_version") != "schednav.rolling-ablation-evidence/v1"
        or not _verified(evidence, "evidence_fingerprint")
    ):
        raise ValueError("Rolling-ablation evidence is invalid")
    evidence_fingerprint = str(evidence["evidence_fingerprint"])
    audit_dir = task_root / audit_task_id
    manager_dir = task_root / manager_task_id
    _validate_meta(audit_dir, audit_task_id, "slo-auditor", project_id)
    _validate_meta(manager_dir, manager_task_id, "manager", project_id)
    audit_isolation = _validate_isolation(
        audit_dir,
        task_id=audit_task_id,
        worker_id="slo-auditor",
        evidence_fingerprint=evidence_fingerprint,
        project_id=project_id,
    )
    manager_isolation = _validate_isolation(
        manager_dir,
        task_id=manager_task_id,
        worker_id="schednav-manager",
        evidence_fingerprint=evidence_fingerprint,
        project_id=project_id,
    )
    audit_stage, audit_wrapper, audit_digest = _validate_stage(
        audit_dir,
        task_id=audit_task_id,
        role="SLO Auditor",
        worker_id="slo-auditor",
        normalized_name="normalized-stage-auditor-closeout-0001.json",
        wrapper_name="analysis-arm-closeout-0001.json",
        payload_key="audit",
        evidence_fingerprint=evidence_fingerprint,
        expected_receipts=1,
    )
    manager_stage, manager_wrapper, manager_digest = _validate_stage(
        manager_dir,
        task_id=manager_task_id,
        role="Manager",
        worker_id="schednav-manager",
        normalized_name="normalized-stage-manager-closeout-0001.json",
        wrapper_name="rolling-decision-record.json",
        payload_key="decision",
        evidence_fingerprint=evidence_fingerprint,
        expected_receipts=2,
    )
    expected_counts = {
        arm_id: int(value["hard_slo_pass_count"])
        for arm_id, value in evidence["arms"].items()
    }
    verification_counts = _expected_verification_counts(evidence)
    audit = audit_stage.get("audit")
    audit_checks = [
        audit.get("evidence_fingerprint_verified") is not True,
        audit.get("record_fingerprint_verified_count")
        != verification_counts["record"],
        audit.get("deterministic_repetition_verified_count")
        != verification_counts["deterministic_repetition"],
        audit.get("rolling_boundary_verified_count")
        != verification_counts["rolling_boundary"],
        audit.get("hard_slo_pass_counts") != expected_counts,
        audit.get("multi_agent_superiority_gate")
        != evidence["multi_agent_superiority_gate"],
        audit.get("multi_agent_vs_ordinary_gate")
        != evidence["multi_agent_vs_ordinary_gate"],
        not isinstance(audit.get("conclusion"), str),
        not bool(audit.get("conclusion")),
    ] if isinstance(audit, dict) else [True]
    if "analyst_causal_value_gate" in evidence and isinstance(audit, dict):
        audit_checks.extend(
            [
                audit.get("analyst_causal_value_gate")
                != evidence["analyst_causal_value_gate"],
                audit.get("analyst_causal_pairwise_hierarchy")
                != evidence.get("analyst_causal_pairwise_hierarchy"),
                audit.get("analyst_causal_matched_resources")
                != evidence.get("analyst_causal_matched_resources"),
            ]
        )
    if any(audit_checks):
        raise ValueError("SLO Auditor result does not match the verified evidence")
    manager_receipts = manager_wrapper["agent_stage_receipts"]
    if manager_receipts[0] != {
        "role": "SLO Auditor",
        "worker_id": "slo-auditor",
        "task_id": audit_task_id,
        "output_fingerprint": audit_digest,
    }:
        raise ValueError("Manager decision is not bound to the Auditor output")
    if manager_wrapper.get("evidence_fingerprint") != evidence_fingerprint:
        raise ValueError("Manager decision is not bound to the rolling evidence")
    eligible = _expected_eligible(evidence)
    recommendation = _rank(evidence, eligible)
    expected_claim = (
        "supported"
        if evidence["multi_agent_superiority_gate"] == "supported"
        and evidence["multi_agent_vs_ordinary_gate"] == "supported"
        else "not_established"
    )
    decision = manager_stage.get("decision")
    decision_checks = [
        decision.get("eligible_deployable_arm_ids") != eligible,
        decision.get("recommended_arm_id") != recommendation,
        decision.get("multi_agent_superiority_gate")
        != evidence["multi_agent_superiority_gate"],
        decision.get("multi_agent_vs_ordinary_gate")
        != evidence["multi_agent_vs_ordinary_gate"],
        decision.get("scheduling_superiority_claim") != expected_claim,
        decision.get("approval_status") != "approval_pending",
        decision.get("production_change_applied") is not False,
        not isinstance(decision.get("rationale"), str),
        not bool(decision.get("rationale")),
    ] if isinstance(decision, dict) else [True]
    if "analyst_causal_value_gate" in evidence and isinstance(decision, dict):
        decision_checks.append(
            decision.get("analyst_causal_value_claim")
            != evidence["analyst_causal_value_gate"]
        )
    if any(decision_checks):
        raise ValueError("Manager decision does not follow the frozen hierarchy")
    receipt: dict[str, Any] = {
        "schema_version": "schednav.rolling-agentteams-closeout/v1",
        "study_id": evidence["study_id"],
        "project_id": project_id,
        "model_id": MODEL_ID,
        "evidence_fingerprint": evidence_fingerprint,
        "stages": {
            "slo_auditor": {
                "task_id": audit_task_id,
                "role": "SLO Auditor",
                "worker_id": "slo-auditor",
                "output_fingerprint": audit_digest,
                **(
                    {"context_isolation_fingerprint": audit_isolation}
                    if audit_isolation
                    else {}
                ),
            },
            "manager": {
                "task_id": manager_task_id,
                "role": "Manager",
                "worker_id": "schednav-manager",
                "output_fingerprint": manager_digest,
                **(
                    {"context_isolation_fingerprint": manager_isolation}
                    if manager_isolation
                    else {}
                ),
            },
        },
        "audit": audit,
        "decision": decision,
        "llm_call_count": 2,
        "human_approval": {
            "status": "approval_pending",
            "production_change_applied": False,
        },
    }
    receipt["receipt_fingerprint"] = canonical_sha256(receipt)
    return receipt


def _write_new(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite published evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument(
        "--evidence",
        default="evidence/rolling-v1/alibaba-gpu-series-2-rolling-ablation-v1.json",
    )
    parser.add_argument("--task-root", required=True)
    parser.add_argument("--audit-task-id", required=True)
    parser.add_argument("--manager-task-id", required=True)
    parser.add_argument("--project-id", default=PROJECT_ID_DEFAULT)
    parser.add_argument(
        "--output",
        default=(
            "evidence/rolling-v1/"
            "alibaba-gpu-series-2-rolling-agentteams-closeout-v1.json"
        ),
    )
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    receipt = build_receipt(
        evidence_path=(root / args.evidence).resolve(),
        task_root=(root / args.task_root).resolve(),
        audit_task_id=args.audit_task_id,
        manager_task_id=args.manager_task_id,
        project_id=args.project_id,
    )
    output = (root / args.output).resolve()
    _write_new(output, receipt)
    print(
        json.dumps(
            {
                "output": args.output,
                "receipt_fingerprint": receipt["receipt_fingerprint"],
                "recommended_arm_id": receipt["decision"]["recommended_arm_id"],
                "approval_status": receipt["human_approval"]["status"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
