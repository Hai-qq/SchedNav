"""Validate and publish one compact rolling-ablation evidence receipt."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from schednav.contracts import canonical_sha256


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _verified(value: dict[str, Any], field: str) -> bool:
    supplied = value.get(field)
    payload = {key: item for key, item in value.items() if key != field}
    return isinstance(supplied, str) and canonical_sha256(payload) == supplied


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


def _validate_llm_stage_accounting(
    arms: dict[str, Any], minimum_calls: dict[str, int] | None = None
) -> None:
    minimum_calls = minimum_calls or {
        "rolling-single-agent": 30,
        "rolling-multi-agent": 60,
    }
    for arm_id, minimum in minimum_calls.items():
        value = arms.get(arm_id, {}).get("llm_call_count")
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError("Rolling Agent LLM-stage accounting is incomplete")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument(
        "--input",
        default="artifacts/rolling-ablation-v1/rolling-ablation-evidence.json",
    )
    parser.add_argument(
        "--output",
        default=(
            "evidence/rolling-v1/"
            "alibaba-gpu-series-2-rolling-ablation-v1.json"
        ),
    )
    parser.add_argument(
        "--study", default="configs/studies/rolling-ablation-v1.json"
    )
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    evidence = _load((root / args.input).resolve())
    study = _load((root / args.study).resolve())
    if (
        study.get("schema_version") != "schednav.rolling-ablation-study/v1"
        or not _verified(study, "design_fingerprint")
        or evidence.get("design_fingerprint") != study.get("design_fingerprint")
    ):
        raise ValueError("Rolling study does not match the evidence")
    if evidence.get("schema_version") != "schednav.rolling-ablation-evidence/v1" or not _verified(
        evidence, "evidence_fingerprint"
    ):
        raise ValueError("Rolling evidence is invalid")
    window_count = len(study.get("holdout_windows", []))
    expected_arms = {str(item["arm_id"]) for item in study.get("arms", [])}
    expected_record_count = window_count * len(expected_arms)
    if (
        evidence.get("record_count") != expected_record_count
        or evidence.get("window_count") != window_count
    ):
        raise ValueError("Rolling evidence is incomplete")
    if evidence.get("multi_agent_superiority_gate") not in {
        "supported",
        "not_established",
    }:
        raise ValueError("Rolling superiority claim gate is incomplete")
    if evidence.get("multi_agent_vs_ordinary_gate") not in {
        "supported",
        "not_established",
    }:
        raise ValueError("Rolling comparison with ordinary FIFO is incomplete")
    if "rolling-multi-agent-masked" in expected_arms and evidence.get(
        "analyst_causal_value_gate"
    ) not in {"supported", "not_established"}:
        raise ValueError("Rolling Analyst causal-value gate is incomplete")
    if not evidence.get("implementation_fingerprint") or not evidence.get(
        "data_fingerprint"
    ):
        raise ValueError("Rolling evidence is missing its implementation/data chain")
    agentteams = evidence.get("agentteams")
    if (
        not isinstance(agentteams, dict)
        or agentteams.get("framework") != "AgentTeams"
        or agentteams.get("model_id") != "deepseek-v4-flash"
        or agentteams.get("plan_count")
        != window_count
        * sum(
            item.get("mode")
            in {"single_agent", "multi_agent", "multi_agent_masked"}
            for item in study["arms"]
        )
        or agentteams.get("token_count_status") != "unavailable"
    ):
        raise ValueError("Rolling evidence is missing AgentTeams provenance")
    arms = evidence.get("arms", {})
    if set(arms) != expected_arms or any(
        arms[arm_id].get("window_count") != window_count
        for arm_id in expected_arms
    ):
        raise ValueError("Rolling evidence does not cover every frozen arm/window")
    interval = int(study["rolling_contract"]["decision_interval_seconds"])
    total_decisions = sum(
        math.ceil(
            (float(window["end_seconds"]) - float(window["start_seconds"]))
            / interval
        )
        for window in study["holdout_windows"]
    )
    scenario_count = (
        2
        if study["rolling_contract"].get("candidate_scenario_set_id")
        == "dual-forecast-replay-v1"
        else 1
    )
    catalog_size = len(
        _load((root / study["rolling_contract"]["action_space"]).resolve())[
            "profiles"
        ]
    )
    deployable_budget = int(
        study["rolling_contract"]["candidate_budget_per_deployable_decision"]
    )
    expected_candidate_cost = {}
    for arm in study["arms"]:
        mode = arm.get("mode")
        if mode in {
            "workload_rule",
            "single_agent",
            "multi_agent",
            "multi_agent_masked",
        }:
            expected_candidate_cost[str(arm["arm_id"])] = (
                total_decisions * deployable_budget * scenario_count
            )
        elif mode == "catalog_oracle":
            expected_candidate_cost[str(arm["arm_id"])] = (
                total_decisions * catalog_size
            )
    for arm_id, expected in expected_candidate_cost.items():
        if arms[arm_id].get("candidate_simulation_count") != expected:
            raise ValueError(f"Unexpected candidate budget for {arm_id}")
    minimum_calls = {}
    for arm in study["arms"]:
        mode = arm.get("mode")
        if mode == "single_agent":
            minimum_calls[str(arm["arm_id"])] = total_decisions
        elif mode in {"multi_agent", "multi_agent_masked"}:
            minimum_calls[str(arm["arm_id"])] = 2 * total_decisions
    _validate_llm_stage_accounting(arms, minimum_calls)
    _write_new((root / args.output).resolve(), evidence)
    print(
        json.dumps(
            {
                "output": args.output,
                "evidence_fingerprint": evidence["evidence_fingerprint"],
                "multi_agent_superiority_gate": evidence[
                    "multi_agent_superiority_gate"
                ],
                "multi_agent_vs_ordinary_gate": evidence[
                    "multi_agent_vs_ordinary_gate"
                ],
                "analyst_causal_value_gate": evidence.get(
                    "analyst_causal_value_gate"
                ),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
