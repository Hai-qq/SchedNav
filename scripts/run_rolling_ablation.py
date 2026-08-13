"""Run or resume the frozen SchedNav rolling-horizon ablation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from statistics import mean
from typing import Any

from schednav.contracts import canonical_sha256
from schednav.native_simulator import SimulationPolicy
from schednav.native_trace import load_canonical_trace
from schednav.rolling_experiment import (
    load_json_object,
    run_rolling_arm,
    run_static_arm,
)
from schednav.slo import audit_slo_reports


RECORD_SCHEMA = "schednav.rolling-ablation-record/v1"
SUMMARY_SCHEMA = "schednav.rolling-ablation-evidence/v1"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _verified(document: dict[str, Any], field: str) -> bool:
    supplied = document.get(field)
    payload = {key: value for key, value in document.items() if key != field}
    return isinstance(supplied, str) and canonical_sha256(payload) == supplied


def _study(path: Path) -> dict[str, Any]:
    value = load_json_object(path)
    if value.get("schema_version") != "schednav.rolling-ablation-study/v1" or not _verified(
        value, "design_fingerprint"
    ):
        raise ValueError("The rolling study is not a valid frozen design")
    return value


def _implementation_fingerprint(project_root: Path, study: dict[str, Any]) -> str:
    paths = [
        project_root / "src/schednav/native_simulator.py",
        project_root / "src/schednav/native_trace.py",
        project_root / "src/schednav/rolling_control.py",
        project_root / "src/schednav/rolling_experiment.py",
        project_root / "src/schednav/tenant_predictive_control.py",
        project_root / "src/schednav/controller_factory.py",
        project_root / "src/schednav/slo.py",
        project_root / study["rolling_contract"]["action_space"],
        project_root / study["rolling_contract"]["inner_controller"],
        project_root / study["rolling_contract"]["slo"],
    ]
    return canonical_sha256(
        {
            path.relative_to(project_root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in paths
        }
    )


def _metric_values(metrics: dict[str, Any]) -> dict[str, float | int | None]:
    return {
        "allocation_rate_mean": metrics["cluster"]["allocation_rate_mean"],
        "hp_completion_rate": metrics["jobs"]["HP"]["completion_rate"],
        "hp_preempted_job_count": metrics["jobs"]["HP"]["preempted_job_count"],
        "hp_jct_p95_seconds": metrics["jobs"]["HP"]["jct_seconds"]["p95"],
        "hp_queue_p95_seconds": metrics["jobs"]["HP"]["queue_seconds"]["p95"],
        "spot_completion_rate": metrics["jobs"]["Spot"]["completion_rate"],
        "spot_jct_p95_seconds": metrics["jobs"]["Spot"]["jct_seconds"]["p95"],
        "spot_eviction_rate_per_run": metrics["preemption_events"][
            "eviction_rate_per_run"
        ],
        "spot_guarantee_success_rate": metrics["spot_guarantee"]["success_rate"],
    }


def _paths(
    project_root: Path, study_id: str, window_id: str
) -> tuple[Path, Path]:
    root = project_root / "datasets/local" / study_id / "windows" / window_id
    return root / "execution/trace.json", root / "history/trace.json"


def _selected_windows(
    study: dict[str, Any], requested_window_ids: list[str] | None
) -> list[dict[str, Any]]:
    windows = list(study["holdout_windows"])
    if not requested_window_ids:
        return windows
    if len(set(requested_window_ids)) != len(requested_window_ids):
        raise ValueError("--window-id values must be unique")
    by_id = {str(item["window_id"]): item for item in windows}
    unknown = sorted(set(requested_window_ids) - set(by_id))
    if unknown:
        raise ValueError(f"Unknown holdout window IDs: {unknown}")
    return [by_id[window_id] for window_id in requested_window_ids]


def _arm_spec(
    project_root: Path,
    study: dict[str, Any],
    window_id: str,
    arm_id: str,
    plans_dir: Path | None,
) -> dict[str, Any]:
    trace_path, history_path = _paths(
        project_root, str(study["study_id"]), window_id
    )
    trace = load_canonical_trace(trace_path)
    policy_root = project_root / "configs/policies"
    predictor = project_root / study["rolling_contract"]["inner_controller"]
    action_space = project_root / study["rolling_contract"]["action_space"]
    slo_path = project_root / study["rolling_contract"]["slo"]
    if arm_id == "ordinary-fifo":
        result, metrics = run_static_arm(
            trace, SimulationPolicy.load(policy_root / "native-fifo.json")
        )
        return {"result": result, "metrics": metrics, "agent_plan": None}
    if arm_id == "fixed-tenant-predictive":
        result, metrics = run_static_arm(
            trace,
            SimulationPolicy.load(
                policy_root / "native-preemptive-g3600-b09-d0000.json"
            ),
            predictive_controller_path=predictor,
        )
        return {"result": result, "metrics": metrics, "agent_plan": None}
    mode_by_arm = {
        "rolling-workload-rule": "workload_rule",
        "rolling-single-agent": "single_agent",
        "rolling-multi-agent": "multi_agent",
        "rolling-multi-agent-masked": "multi_agent_masked",
        "posthoc-catalog-oracle": "catalog_oracle",
    }
    mode = mode_by_arm[arm_id]
    plan = None
    if mode in {"single_agent", "multi_agent", "multi_agent_masked"}:
        if plans_dir is None:
            raise FileNotFoundError("Agent plan directory is required")
        plan_path = plans_dir / window_id / f"{mode}.json"
        plan = load_json_object(plan_path)
    result, metrics = run_rolling_arm(
        project_root=project_root,
        trace_path=trace_path,
        workload_history_trace_path=history_path,
        action_space_path=action_space,
        predictive_controller_path=predictor,
        slo_path=slo_path,
        arm_id=(
            plan["controller_id"]
            if plan is not None
            else f"{arm_id}-{window_id}"
        ),
        mode=mode,
        agent_plan=plan,
        decision_interval_seconds=int(
            study["rolling_contract"]["decision_interval_seconds"]
        ),
        scenario_horizon_seconds=int(
            study["rolling_contract"]["scenario_horizon_seconds"]
        ),
        history_window_seconds=int(
            study["rolling_contract"]["history_window_seconds"]
        ),
        candidate_budget=int(
            study["rolling_contract"][
                "candidate_budget_per_deployable_decision"
            ]
        ),
        scenario_set_id=str(
            study["rolling_contract"].get(
                "candidate_scenario_set_id", "single-calibrated-p90"
            )
        ),
        minimum_action_hold_seconds=int(
            study["rolling_contract"].get("minimum_action_hold_seconds", 0)
        ),
    )
    return {"result": result, "metrics": metrics, "agent_plan": plan}


def _run_record(
    *,
    project_root: Path,
    study: dict[str, Any],
    implementation_fingerprint: str,
    window_id: str,
    arm_id: str,
    output_dir: Path,
    plans_dir: Path | None,
    baseline_metrics: dict[str, Any],
) -> dict[str, Any]:
    record_path = output_dir / "windows" / window_id / arm_id / "record.json"
    if record_path.is_file():
        existing = load_json_object(record_path)
        if (
            _verified(existing, "record_fingerprint")
            and existing.get("design_fingerprint") == study["design_fingerprint"]
            and existing.get("implementation_fingerprint")
            == implementation_fingerprint
            and existing.get("deterministic_repetitions") is True
        ):
            return existing
        raise ValueError(f"Stale or invalid rolling record: {record_path}")

    repetitions = []
    first_payload: dict[str, Any] | None = None
    for repetition in range(1, int(study["execution"]["repetitions_per_arm_per_window"]) + 1):
        payload = _arm_spec(project_root, study, window_id, arm_id, plans_dir)
        audit = audit_slo_reports(payload["metrics"], load_json_object(
            project_root / study["rolling_contract"]["slo"]
        ), baseline_metrics)
        repetition_dir = record_path.parent / f"repetition-{repetition}"
        _write_json(repetition_dir / "simulation-result.json", payload["result"])
        _write_json(repetition_dir / "metrics.json", payload["metrics"])
        _write_json(repetition_dir / "slo-audit.json", audit)
        if payload["agent_plan"] is not None:
            _write_json(repetition_dir / "agent-plan.json", payload["agent_plan"])
        repetitions.append(
            {
                "repetition": repetition,
                "result_fingerprint": payload["result"]["result_fingerprint"],
                "metrics_fingerprint": payload["metrics"]["metrics_fingerprint"],
                "audit_fingerprint": audit["audit_fingerprint"],
            }
        )
        if first_payload is None:
            first_payload = {**payload, "audit": audit}
    assert first_payload is not None
    deterministic = len(
        {
            (item["result_fingerprint"], item["metrics_fingerprint"], item["audit_fingerprint"])
            for item in repetitions
        }
    ) == 1
    if not deterministic:
        raise RuntimeError(f"Non-deterministic rolling arm: {window_id}/{arm_id}")
    rolling = first_payload["metrics"].get("rolling_control")
    failed = [
        item["id"]
        for item in first_payload["audit"]["results"]
        if item["severity"] == "hard" and not item["passed"]
    ]
    record: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA,
        "design_fingerprint": study["design_fingerprint"],
        "implementation_fingerprint": implementation_fingerprint,
        "window_id": window_id,
        "trace_fingerprint": first_payload["metrics"]["source"]["trace_fingerprint"],
        "arm_id": arm_id,
        "repetition_count": len(repetitions),
        "deterministic_repetitions": deterministic,
        "repetitions": repetitions,
        "hard_slo_passed": first_payload["audit"]["audit_passed"],
        "failed_hard_constraints": failed,
        "metrics": _metric_values(first_payload["metrics"]),
        "rolling": (
            {
                "controller_fingerprint": rolling["controller_fingerprint"],
                "control_fingerprint": rolling["control_fingerprint"],
                "decision_count": rolling["decision_count"],
                "candidate_budget_per_decision": rolling[
                    "candidate_budget_per_decision"
                ],
                "candidate_simulation_count": rolling[
                    "candidate_simulation_count"
                ],
                "selected_action_sequence": rolling["selected_action_sequence"],
                "llm_usage": rolling["llm_usage"],
                "state_handoff": rolling["state_handoff"],
                "information_boundary": rolling["information_boundary"],
            }
            if rolling is not None
            else None
        ),
        "agent_plan_fingerprint": (
            first_payload["agent_plan"]["plan_fingerprint"]
            if first_payload["agent_plan"] is not None
            else None
        ),
    }
    record["record_fingerprint"] = canonical_sha256(record)
    _write_json(record_path, record)
    return record


def _aggregate(
    records: list[dict[str, Any]],
    study: dict[str, Any],
    agentteams: dict[str, Any] | None = None,
    data_fingerprint: str | None = None,
) -> dict[str, Any]:
    implementation_fingerprints = {
        item["implementation_fingerprint"]
        for item in records
        if "implementation_fingerprint" in item
    }
    if len(implementation_fingerprints) > 1:
        raise ValueError("Rolling evidence mixes simulator implementations")
    arm_ids = [item["arm_id"] for item in study["arms"]]
    by_arm = {arm_id: [item for item in records if item["arm_id"] == arm_id] for arm_id in arm_ids}
    arms: dict[str, Any] = {}
    for arm_id, items in by_arm.items():
        if not items:
            continue
        numeric = ("allocation_rate_mean", "spot_jct_p95_seconds", "spot_eviction_rate_per_run")
        arms[arm_id] = {
            "window_count": len(items),
            "hard_slo_pass_count": sum(item["hard_slo_passed"] for item in items),
            "mean_metrics": {
                key: mean(
                    float(item["metrics"][key])
                    for item in items
                    if item["metrics"][key] is not None
                )
                for key in numeric
            },
            "candidate_simulation_count": sum(
                int(item["rolling"]["candidate_simulation_count"])
                for item in items
                if item["rolling"] is not None
            ),
            "llm_call_count": sum(
                int(item["rolling"]["llm_usage"]["call_count"])
                for item in items
                if item["rolling"] is not None
            ),
            "record_fingerprints": [item["record_fingerprint"] for item in items],
        }

    def compare(left: dict[str, Any], right: dict[str, Any]) -> str:
        if left["hard_slo_pass_count"] != right["hard_slo_pass_count"]:
            return "better" if left["hard_slo_pass_count"] > right["hard_slo_pass_count"] else "worse"
        allocation_delta = (
            left["mean_metrics"]["allocation_rate_mean"]
            - right["mean_metrics"]["allocation_rate_mean"]
        )
        if abs(allocation_delta) >= 0.01:
            return "better" if allocation_delta > 0 else "worse"
        spot_delta = (
            left["mean_metrics"]["spot_jct_p95_seconds"]
            - right["mean_metrics"]["spot_jct_p95_seconds"]
        )
        if spot_delta:
            return "better" if spot_delta < 0 else "worse"
        eviction_delta = (
            left["mean_metrics"]["spot_eviction_rate_per_run"]
            - right["mean_metrics"]["spot_eviction_rate_per_run"]
        )
        if eviction_delta:
            return "better" if eviction_delta < 0 else "worse"
        return "tie"

    comparisons = {}
    gate = "incomplete"
    ordinary_gate = "incomplete"
    analyst_causal_gate = "incomplete"
    analyst_causal_comparison = "incomplete"
    analyst_causal_resources: dict[str, Any] | None = None
    if "rolling-multi-agent" in arms:
        multi = arms["rolling-multi-agent"]
        comparators = [
            "ordinary-fifo",
            "fixed-tenant-predictive",
            "rolling-workload-rule",
            "rolling-single-agent",
        ]
        if "rolling-multi-agent-masked" in arms:
            comparators.append("rolling-multi-agent-masked")
        for comparator in comparators:
            if comparator in arms:
                comparisons[comparator] = compare(multi, arms[comparator])
        required = {
            "ordinary-fifo",
            "fixed-tenant-predictive",
            "rolling-workload-rule",
            "rolling-single-agent",
        }
        if "rolling-multi-agent-masked" in arms:
            required.add("rolling-multi-agent-masked")
        if required.issubset(comparisons):
            no_fewer = all(
                multi["hard_slo_pass_count"] >= arms[item]["hard_slo_pass_count"]
                for item in required
            )
            same_budget_comparators = [
                "rolling-workload-rule",
                "rolling-single-agent",
            ]
            if "rolling-multi-agent-masked" in arms:
                same_budget_comparators.append("rolling-multi-agent-masked")
            equal_candidate_simulation_budget = all(
                multi["candidate_simulation_count"]
                == arms[item]["candidate_simulation_count"]
                for item in same_budget_comparators
            )
            same_budget_better = all(
                comparisons[item] == "better"
                for item in same_budget_comparators
            )
            gate = (
                "supported"
                if no_fewer
                and equal_candidate_simulation_budget
                and same_budget_better
                else "not_established"
            )
        if "ordinary-fifo" in comparisons:
            ordinary_gate = (
                "supported"
                if comparisons["ordinary-fifo"] == "better"
                else "not_established"
            )
        if "rolling-multi-agent-masked" in arms:
            masked = arms["rolling-multi-agent-masked"]
            analyst_causal_comparison = comparisons[
                "rolling-multi-agent-masked"
            ]
            analyst_causal_resources = {
                "window_count_equal": (
                    multi["window_count"] == masked["window_count"]
                ),
                "candidate_simulation_count_equal": (
                    multi["candidate_simulation_count"]
                    == masked["candidate_simulation_count"]
                ),
                "llm_call_count_equal": (
                    multi["llm_call_count"] == masked["llm_call_count"]
                ),
                "multi_agent": {
                    "window_count": multi["window_count"],
                    "candidate_simulation_count": multi[
                        "candidate_simulation_count"
                    ],
                    "llm_call_count": multi["llm_call_count"],
                },
                "masked_handoff": {
                    "window_count": masked["window_count"],
                    "candidate_simulation_count": masked[
                        "candidate_simulation_count"
                    ],
                    "llm_call_count": masked["llm_call_count"],
                },
            }
            resources_match = all(
                analyst_causal_resources[field]
                for field in (
                    "window_count_equal",
                    "candidate_simulation_count_equal",
                    "llm_call_count_equal",
                )
            )
            analyst_causal_gate = (
                "supported"
                if resources_match and analyst_causal_comparison == "better"
                else "not_established"
            )
    value: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA,
        "study_id": study["study_id"],
        "design_fingerprint": study["design_fingerprint"],
        "data_fingerprint": data_fingerprint,
        "implementation_fingerprint": (
            next(iter(implementation_fingerprints))
            if implementation_fingerprints
            else None
        ),
        "prompt_fingerprint": study["freeze"]["prompt_fingerprint"],
        "window_count": len(study["holdout_windows"]),
        "holdout_window_ids": [
            item["window_id"] for item in study["holdout_windows"]
        ],
        "record_count": len(records),
        "agentteams": agentteams,
        "arms": arms,
        "multi_agent_pairwise_hierarchy": comparisons,
        "multi_agent_superiority_gate": gate,
        "multi_agent_vs_ordinary_gate": ordinary_gate,
        "analyst_causal_value_gate": analyst_causal_gate,
        "analyst_causal_pairwise_hierarchy": analyst_causal_comparison,
        "analyst_causal_matched_resources": analyst_causal_resources,
        "definition": (
            "More hard-SLO-passing windows ranks first; with equal counts, allocation "
            "differences below one percentage point defer to Spot p95 JCT, then eviction."
        ),
    }
    value["evidence_fingerprint"] = canonical_sha256(value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--study", default="configs/studies/rolling-ablation-v1.json")
    parser.add_argument(
        "--data-contract",
        default="configs/studies/rolling-ablation-data-v1.json",
    )
    parser.add_argument("--output-dir", default="artifacts/rolling-ablation-v1")
    parser.add_argument("--plans-dir")
    parser.add_argument("--agentteams-project-id")
    parser.add_argument(
        "--phase",
        choices=("non-agent", "agent", "all", "summarize"),
        default="all",
    )
    parser.add_argument(
        "--window-id",
        action="append",
        help="Run only this frozen holdout window; repeat for multiple windows.",
    )
    parser.add_argument(
        "--defer-summary",
        action="store_true",
        help="Write window records only; use --phase summarize after parallel workers finish.",
    )
    args = parser.parse_args()
    project_root = Path(args.project_root).resolve()
    study = _study((project_root / args.study).resolve())
    data_contract = load_json_object((project_root / args.data_contract).resolve())
    if (
        data_contract.get("schema_version") != "schednav.rolling-ablation-data/v1"
        or not _verified(data_contract, "data_fingerprint")
        or data_contract.get("design_fingerprint") != study["design_fingerprint"]
    ):
        raise ValueError("Rolling data contract is invalid")
    output_dir = (project_root / args.output_dir).resolve()
    plans_dir = (project_root / args.plans_dir).resolve() if args.plans_dir else None
    agentteams = None
    if args.phase in {"agent", "all"} and (
        plans_dir is None or not args.agentteams_project_id
    ):
        raise ValueError("Agent phase requires --plans-dir and --agentteams-project-id")
    if plans_dir is not None or args.agentteams_project_id:
        if plans_dir is None or not args.agentteams_project_id:
            raise ValueError(
                "--plans-dir and --agentteams-project-id must be supplied together"
            )
        manifest = load_json_object(plans_dir / "manifest.json")
        if (
            manifest.get("schema_version") != "schednav.rolling-agent-plan-set/v1"
            or not _verified(manifest, "manifest_fingerprint")
            or manifest.get("project_id") != args.agentteams_project_id
            or manifest.get("design_fingerprint") != study["design_fingerprint"]
            or manifest.get("model_id") != "deepseek-v4-flash"
            or manifest.get("stage_receipt_verification", {}).get("status")
            != "verified"
            or manifest.get("stage_receipt_verification", {}).get(
                "context_isolation"
            )
            != "verified"
            or len(manifest.get("plans", []))
            != len(study["holdout_windows"])
            * sum(
                item.get("mode")
                in {"single_agent", "multi_agent", "multi_agent_masked"}
                for item in study["arms"]
            )
        ):
            raise ValueError("AgentTeams rolling plan manifest is invalid")
        agentteams = {
            "framework": "AgentTeams",
            "project_id": args.agentteams_project_id,
            "model_id": "deepseek-v4-flash",
            "plan_manifest_fingerprint": manifest["manifest_fingerprint"],
            "plan_count": len(manifest["plans"]),
            "token_count_status": "unavailable",
        }
    implementation = _implementation_fingerprint(project_root, study)
    all_records: list[dict[str, Any]] = []
    selected_windows = _selected_windows(study, args.window_id)
    for window in selected_windows:
        window_id = window["window_id"]
        baseline_record_path = (
            output_dir / "windows" / window_id / "ordinary-fifo" / "record.json"
        )
        if args.phase in {"non-agent", "all"} or not baseline_record_path.is_file():
            trace_path, _ = _paths(
                project_root, str(study["study_id"]), window_id
            )
            trace = load_canonical_trace(trace_path)
            baseline_result, baseline_metrics = run_static_arm(
                trace,
                SimulationPolicy.load(project_root / "configs/policies/native-fifo.json"),
            )
            del baseline_result
            _run_record(
                project_root=project_root,
                study=study,
                implementation_fingerprint=implementation,
                window_id=window_id,
                arm_id="ordinary-fifo",
                output_dir=output_dir,
                plans_dir=plans_dir,
                baseline_metrics=baseline_metrics,
            )
        baseline_metrics = load_json_object(
            baseline_record_path.parent / "repetition-1" / "metrics.json"
        )
        selected_arms = []
        if args.phase in {"non-agent", "all"}:
            selected_arms.extend(
                [
                    "fixed-tenant-predictive",
                    "rolling-workload-rule",
                    "posthoc-catalog-oracle",
                ]
            )
        if args.phase in {"agent", "all"}:
            selected_arms.extend(
                [
                    item["arm_id"]
                    for item in study["arms"]
                    if item.get("mode")
                    in {"single_agent", "multi_agent", "multi_agent_masked"}
                ]
            )
        for arm_id in selected_arms:
            record = _run_record(
                project_root=project_root,
                study=study,
                implementation_fingerprint=implementation,
                window_id=window_id,
                arm_id=arm_id,
                output_dir=output_dir,
                plans_dir=plans_dir,
                baseline_metrics=baseline_metrics,
            )
            print(
                json.dumps(
                    {
                        "window_id": window_id,
                        "arm_id": arm_id,
                        "hard_slo_passed": record["hard_slo_passed"],
                        "record_fingerprint": record["record_fingerprint"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
    for path in sorted((output_dir / "windows").glob("*/*/record.json")):
        value = load_json_object(path)
        if _verified(value, "record_fingerprint"):
            all_records.append(value)
    if args.defer_summary:
        print(
            json.dumps(
                {
                    "status": "window_records_completed",
                    "window_ids": [item["window_id"] for item in selected_windows],
                    "summary_deferred": True,
                },
                sort_keys=True,
            )
        )
        return 0
    summary = _aggregate(
        all_records,
        study,
        agentteams,
        data_contract["data_fingerprint"],
    )
    _write_json(output_dir / "rolling-ablation-evidence.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
