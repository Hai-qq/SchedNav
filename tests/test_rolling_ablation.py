from pathlib import Path
import hashlib
import importlib.util
import json
import tempfile
import unittest

from scripts.collect_agentteams_rolling_plans import (
    _load_task_meta,
    _verify_context_isolation,
    _verify_stage_receipt,
)
from scripts.prepare_rolling_ablation import _build_data_contract
from scripts.publish_rolling_ablation import _validate_llm_stage_accounting
from scripts.publish_rolling_agentteams_closeout import (
    _expected_eligible,
    _expected_verification_counts,
    _rank,
    _validate_meta,
)
from scripts.run_rolling_ablation import _aggregate, _selected_windows
from schednav.contracts import canonical_sha256


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_execute_wave_module():
    path = PROJECT_ROOT / "artifacts" / "agentteams-execute-wave-v3.py"
    spec = importlib.util.spec_from_file_location("agentteams_execute_wave_v3", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(arm_id: str, *, spot_jct: float) -> dict:
    return {
        "arm_id": arm_id,
        "hard_slo_passed": True,
        "metrics": {
            "allocation_rate_mean": 0.75,
            "spot_jct_p95_seconds": spot_jct,
            "spot_eviction_rate_per_run": 0.01,
        },
        "rolling": None,
        "record_fingerprint": (arm_id.encode("utf-8").hex() + "0" * 64)[:64],
    }


class RollingAblationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.study = {
            "study_id": "fixture",
            "design_fingerprint": "a" * 64,
            "freeze": {"prompt_fingerprint": "b" * 64},
            "holdout_windows": [{"window_id": "w1"}],
            "arms": [
                {"arm_id": arm_id}
                for arm_id in (
                    "ordinary-fifo",
                    "fixed-tenant-predictive",
                    "rolling-workload-rule",
                    "rolling-single-agent",
                    "rolling-multi-agent",
                )
            ],
        }

    def test_artifact_visibility_404_is_retryable_only_for_reads(self) -> None:
        module = _load_execute_wave_module()
        response = {"error": "SSE error: Non-200 status code (404)"}
        self.assertTrue(module.transient_mcp_error("read_artifact", response))
        self.assertFalse(module.transient_mcp_error("advance_rolling_policy", response))

    def test_large_artifacts_use_compact_text_transport(self) -> None:
        module = _load_execute_wave_module()
        self.assertEqual(module.mcp_output_mode("read_artifact"), "text")
        self.assertEqual(module.mcp_output_mode("get_task"), "json")

    def test_exact_mcporter_cap_becomes_explicit_omission_receipt(self) -> None:
        module = _load_execute_wave_module()
        receipt = module.truncated_artifact_receipt(
            "read_artifact",
            {"artifact_ref": "tasks/x/report.json"},
            '{"artifact_ref"' + "x" * 65_522,
        )
        self.assertEqual(receipt["read_status"], "omitted_mcporter_output_limit")
        self.assertTrue(module.artifact_read_satisfied("rolling_control_report", receipt))
        self.assertFalse(module.artifact_read_satisfied("metrics", receipt))

    def test_semantic_artifact_errors_are_not_retryable(self) -> None:
        module = _load_execute_wave_module()
        self.assertFalse(
            module.transient_mcp_error(
                "read_artifact", {"error": {"message": "invalid artifact_ref"}}
            )
        )

    def test_task_meta_literal_newline_is_disclosed_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "meta.json"
            original = '{"task_id":"t"}\\n'
            path.write_text(original, encoding="utf-8")
            value, normalized = _load_task_meta(path)
            self.assertEqual(value, {"task_id": "t"})
            self.assertTrue(normalized)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_contract_schemas_are_json_schema_documents(self) -> None:
        for name in (
            "agentteams-context-isolation.schema.json",
            "rolling-ablation-study.schema.json",
            "rolling-ablation-data.schema.json",
            "rolling-ablation-preparation.schema.json",
            "rolling-ablation-record.schema.json",
            "rolling-ablation-evidence.schema.json",
            "rolling-agentteams-closeout.schema.json",
            "rolling-agent-plan.schema.json",
            "rolling-agent-plan-set.schema.json",
            "rolling-control-report.schema.json",
            "rolling-planning-checkpoint.schema.json",
            "rolling-run.schema.json",
        ):
            schema = json.loads(
                (PROJECT_ROOT / "schemas" / name).read_text(encoding="utf-8")
            )
            self.assertEqual(
                schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
            )

    def test_publisher_preserves_llm_retry_and_correction_calls(self) -> None:
        _validate_llm_stage_accounting(
            {
                "rolling-single-agent": {"llm_call_count": 31},
                "rolling-multi-agent": {"llm_call_count": 61},
            }
        )

    def test_publisher_rejects_missing_llm_stages(self) -> None:
        with self.assertRaisesRegex(ValueError, "accounting is incomplete"):
            _validate_llm_stage_accounting(
                {
                    "rolling-single-agent": {"llm_call_count": 29},
                    "rolling-multi-agent": {"llm_call_count": 60},
                }
            )

    def test_closeout_uses_only_all_window_deployable_arms(self) -> None:
        evidence = {
            "window_count": 5,
            "arms": {
                "ordinary-fifo": {
                    "hard_slo_pass_count": 5,
                    "mean_metrics": {
                        "allocation_rate_mean": 0.65,
                        "spot_jct_p95_seconds": 48_000.0,
                        "spot_eviction_rate_per_run": 0.0,
                    },
                },
                "rolling-multi-agent": {
                    "hard_slo_pass_count": 1,
                    "mean_metrics": {
                        "allocation_rate_mean": 0.64,
                        "spot_jct_p95_seconds": 47_000.0,
                        "spot_eviction_rate_per_run": 0.01,
                    },
                },
                "posthoc-catalog-oracle": {
                    "hard_slo_pass_count": 5,
                    "mean_metrics": {
                        "allocation_rate_mean": 0.80,
                        "spot_jct_p95_seconds": 40_000.0,
                        "spot_eviction_rate_per_run": 0.0,
                    },
                },
            },
        }
        eligible = _expected_eligible(evidence)
        self.assertEqual(eligible, ["ordinary-fifo"])
        self.assertEqual(_rank(evidence, eligible), "ordinary-fifo")

    def test_closeout_recommends_nothing_when_every_arm_misses_a_window(self) -> None:
        evidence = json.loads(
            (
                PROJECT_ROOT
                / "evidence"
                / "rolling-v2"
                / "alibaba-gpu-series-2-rolling-ablation-v2.json"
            ).read_text(encoding="utf-8")
        )

        eligible = _expected_eligible(evidence)

        self.assertEqual(eligible, [])
        self.assertIsNone(_rank(evidence, eligible))

    def test_closeout_counts_follow_evidence_shape(self) -> None:
        evidence = {
            "window_count": 5,
            "record_count": 35,
            "arms": {
                "ordinary-fifo": {"candidate_simulation_count": 0},
                "rolling-workload-rule": {"candidate_simulation_count": 360},
                "rolling-single-agent": {"candidate_simulation_count": 360},
                "rolling-multi-agent": {"candidate_simulation_count": 360},
                "rolling-multi-agent-masked": {
                    "candidate_simulation_count": 360
                },
                "posthoc-catalog-oracle": {"candidate_simulation_count": 300},
            },
        }

        self.assertEqual(
            _expected_verification_counts(evidence),
            {
                "record": 35,
                "deterministic_repetition": 35,
                "rolling_boundary": 20,
            },
        )

    def test_closeout_accepts_an_explicit_agentteams_project_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary)
            (task_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "task_id": "task-v2",
                        "project_id": "project-v2",
                        "assigned_to": "slo-auditor",
                        "status": "completed",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            _validate_meta(
                task_dir,
                "task-v2",
                "slo-auditor",
                "project-v2",
            )
            with self.assertRaisesRegex(ValueError, "metadata is invalid"):
                _validate_meta(
                    task_dir,
                    "task-v2",
                    "slo-auditor",
                    "another-project",
                )

    def test_data_preparation_contract_is_content_addressed(self) -> None:
        contract = json.loads(
            (
                PROJECT_ROOT
                / "configs/studies/rolling-ablation-data-v1.json"
            ).read_text(encoding="utf-8")
        )
        supplied = contract.pop("data_fingerprint")
        self.assertEqual(canonical_sha256(contract), supplied)

    def test_window_filter_preserves_frozen_order_requested(self) -> None:
        study = {
            "holdout_windows": [
                {"window_id": "w1"},
                {"window_id": "w2"},
                {"window_id": "w3"},
            ]
        }
        self.assertEqual(
            [
                item["window_id"]
                for item in _selected_windows(study, ["w3", "w1"])
            ],
            ["w3", "w1"],
        )
        with self.assertRaisesRegex(ValueError, "Unknown holdout"):
            _selected_windows(study, ["w4"])
        with self.assertRaisesRegex(ValueError, "unique"):
            _selected_windows(study, ["w1", "w1"])

    def test_new_data_contract_can_be_frozen_without_policy_results(self) -> None:
        study = {
            "study_id": "fixture-v2",
            "design_fingerprint": "a" * 64,
            "data": {
                "dataset": "fixture-trace",
                "source_commit": "b" * 40,
                "time_origin": "2024-03-01 00:00:00",
                "gpu_model": "GPU-series-2",
            },
            "rolling_contract": {"history_window_seconds": 14400},
        }
        windows = [
            {
                "window_id": "w1",
                "execution_trace_fingerprint": "c" * 64,
                "history_trace_fingerprint": "d" * 64,
                "execution_job_count": 10,
                "history_job_count": 3,
            }
        ]
        contract = _build_data_contract(
            study,
            {
                "node_info_sha256": "e" * 64,
                "job_info_sha256": "f" * 64,
            },
            windows,
        )
        supplied = contract.pop("data_fingerprint")
        self.assertEqual(canonical_sha256(contract), supplied)
        self.assertNotIn("metrics", contract)
        self.assertNotIn("simulation", contract)

    def test_superiority_gate_requires_better_same_budget_controllers(self) -> None:
        records = [
            _record("ordinary-fifo", spot_jct=120),
            _record("fixed-tenant-predictive", spot_jct=115),
            _record("rolling-workload-rule", spot_jct=110),
            _record("rolling-single-agent", spot_jct=105),
            _record("rolling-multi-agent", spot_jct=90),
        ]
        evidence = _aggregate(records, self.study)
        self.assertEqual(evidence["multi_agent_superiority_gate"], "supported")
        self.assertEqual(evidence["multi_agent_vs_ordinary_gate"], "supported")
        records[-1]["metrics"]["spot_jct_p95_seconds"] = 105
        evidence = _aggregate(records, self.study)
        self.assertEqual(evidence["multi_agent_superiority_gate"], "not_established")
        self.assertEqual(evidence["multi_agent_vs_ordinary_gate"], "supported")
        records[-1]["metrics"]["spot_jct_p95_seconds"] = 120
        evidence = _aggregate(records, self.study)
        self.assertEqual(evidence["multi_agent_vs_ordinary_gate"], "not_established")

    def test_analyst_causal_gate_requires_matched_resources_and_strict_gain(self) -> None:
        self.study["arms"].append(
            {"arm_id": "rolling-multi-agent-masked"}
        )
        records = [
            _record("ordinary-fifo", spot_jct=120),
            _record("fixed-tenant-predictive", spot_jct=115),
            _record("rolling-workload-rule", spot_jct=110),
            _record("rolling-single-agent", spot_jct=105),
            _record("rolling-multi-agent", spot_jct=90),
            _record("rolling-multi-agent-masked", spot_jct=100),
        ]
        for item in records[-2:]:
            item["rolling"] = {
                "candidate_simulation_count": 6,
                "llm_usage": {"call_count": 2},
            }

        evidence = _aggregate(records, self.study)

        self.assertEqual(evidence["analyst_causal_value_gate"], "supported")
        self.assertEqual(
            evidence["analyst_causal_pairwise_hierarchy"], "better"
        )
        self.assertTrue(
            evidence["analyst_causal_matched_resources"]["llm_call_count_equal"]
        )

        records[-1]["rolling"]["llm_usage"]["call_count"] = 3
        evidence = _aggregate(records, self.study)
        self.assertEqual(
            evidence["analyst_causal_value_gate"], "not_established"
        )

    def test_stage_receipt_is_bound_to_exported_agentteams_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary)
            task_id = "task-20260810-063100"
            workspace = task_root / task_id / "workspace"
            workspace.mkdir(parents=True)
            (task_root / task_id / "meta.json").write_text(
                json.dumps(
                    {
                        "task_id": task_id,
                        "project_id": "fixture-project",
                        "assigned_to": "scheduling-strategist",
                        "status": "submitted",
                        "room_id": "private-room",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            isolation = {
                "schema_version": "schednav.agentteams-context-isolation/v1",
                "project_id": "fixture-project",
                "task_id": task_id,
                "worker_id": "scheduling-strategist",
                "worker_private_room_id": "private-room",
                "project_room_id": "project-room",
                "scope": "one-controller-one-observation",
                "observation_fingerprint": "a" * 64,
                "clear_command": "/clear",
                "clear_acknowledged": True,
                "clear_acknowledged_at": "2026-08-10T00:00:00Z",
                "assignment_dispatched_at": "2026-08-10T00:00:01Z",
                "assignment_context_verified": True,
                "assignment_context_evidence": "worker-log-handle-agent-query",
                "cross_window_context_visible": False,
            }
            isolation["receipt_fingerprint"] = canonical_sha256(isolation)
            (task_root / task_id / "context-isolation.json").write_text(
                json.dumps(isolation) + "\n", encoding="utf-8"
            )
            stage = {
                "schema_version": "schednav.agent-stage-output/v1",
                "observation_fingerprint": "a" * 64,
                "model_id": "deepseek-v4-flash",
                "role": "Scheduling Strategist",
                "worker_id": "scheduling-strategist",
                "task_id": task_id,
                "candidate_action_ids": [
                    "native-fifo",
                    "rolling-fifo-open",
                    "rolling-preemptive-open-d0000",
                ],
                "reason_code": "fixture",
            }
            stage_path = workspace / "normalized-stage-strategist.json"
            stage_path.write_text(json.dumps(stage) + "\n", encoding="utf-8")
            receipt = {
                "role": stage["role"],
                "worker_id": stage["worker_id"],
                "task_id": task_id,
                "output_fingerprint": hashlib.sha256(
                    stage_path.read_bytes()
                ).hexdigest(),
            }
            self.assertEqual(
                _verify_stage_receipt(
                    receipt,
                    project_id="fixture-project",
                    observation_fingerprint=stage["observation_fingerprint"],
                    candidate_action_ids=stage["candidate_action_ids"],
                    reason_code=stage["reason_code"],
                    task_root=task_root,
                ),
                stage_path,
            )
            receipt["output_fingerprint"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                _verify_stage_receipt(
                    receipt,
                    project_id="fixture-project",
                    observation_fingerprint=stage["observation_fingerprint"],
                    candidate_action_ids=stage["candidate_action_ids"],
                    reason_code=stage["reason_code"],
                    task_root=task_root,
                )

    def test_stage_receipt_rejects_assignment_before_context_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary)
            task_id = "task-20260810-063100"
            task_dir = task_root / task_id
            workspace = task_dir / "workspace"
            workspace.mkdir(parents=True)
            (task_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "task_id": task_id,
                        "project_id": "fixture-project",
                        "assigned_to": "scheduling-strategist",
                        "status": "submitted",
                        "room_id": "private-room",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            isolation = {
                "schema_version": "schednav.agentteams-context-isolation/v1",
                "project_id": "fixture-project",
                "task_id": task_id,
                "worker_id": "scheduling-strategist",
                "worker_private_room_id": "private-room",
                "project_room_id": "project-room",
                "scope": "one-controller-one-observation",
                "observation_fingerprint": "a" * 64,
                "clear_command": "/clear",
                "clear_acknowledged": True,
                "clear_acknowledged_at": "2026-08-10T00:00:02Z",
                "assignment_dispatched_at": "2026-08-10T00:00:01Z",
                "assignment_context_verified": True,
                "assignment_context_evidence": "worker-log-handle-agent-query",
                "cross_window_context_visible": False,
            }
            isolation["receipt_fingerprint"] = canonical_sha256(isolation)
            (task_dir / "context-isolation.json").write_text(
                json.dumps(isolation) + "\n", encoding="utf-8"
            )
            stage = {
                "schema_version": "schednav.agent-stage-output/v1",
                "observation_fingerprint": "a" * 64,
                "model_id": "deepseek-v4-flash",
                "role": "Scheduling Strategist",
                "worker_id": "scheduling-strategist",
                "task_id": task_id,
                "candidate_action_ids": [
                    "native-fifo",
                    "rolling-fifo-open",
                    "rolling-preemptive-open-d0000",
                ],
                "reason_code": "fixture",
            }
            stage_path = workspace / "normalized-stage-strategist.json"
            stage_path.write_text(json.dumps(stage) + "\n", encoding="utf-8")
            receipt = {
                "role": stage["role"],
                "worker_id": stage["worker_id"],
                "task_id": task_id,
                "output_fingerprint": hashlib.sha256(stage_path.read_bytes()).hexdigest(),
            }
            with self.assertRaisesRegex(ValueError, "assigned before context clear"):
                _verify_stage_receipt(
                    receipt,
                    project_id="fixture-project",
                    observation_fingerprint=stage["observation_fingerprint"],
                    candidate_action_ids=stage["candidate_action_ids"],
                    reason_code=stage["reason_code"],
                    task_root=task_root,
                )

    def test_fresh_room_context_isolation_receipt_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary)
            task_id = "task-fresh-room"
            meta = {"room_id": "fresh-private-room"}
            isolation = {
                "schema_version": "schednav.agentteams-context-isolation/v2",
                "project_id": "fixture-project",
                "task_id": task_id,
                "worker_id": "scheduling-strategist",
                "worker_private_room_id": "fresh-private-room",
                "project_room_id": "project-room",
                "scope": "one-controller-one-observation",
                "observation_fingerprint": "a" * 64,
                "isolation_method": "fresh-private-room-single-assignment",
                "fresh_room_created_for_task": True,
                "fresh_room_verified_at": "2026-08-13T00:00:00Z",
                "assignment_dispatched_at": "2026-08-13T00:00:01Z",
                "assignment_context_verified": True,
                "assignment_context_evidence": (
                    "fresh-private-room-single-assignment"
                ),
                "cross_window_context_visible": False,
            }
            isolation["receipt_fingerprint"] = canonical_sha256(isolation)
            (task_dir / "context-isolation.json").write_text(
                json.dumps(isolation) + "\n", encoding="utf-8"
            )

            _verify_context_isolation(
                task_dir,
                meta=meta,
                project_id="fixture-project",
                task_id=task_id,
                worker_id="scheduling-strategist",
                observation_fingerprint="a" * 64,
            )

    def test_fresh_room_receipt_rejects_dispatch_before_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary)
            task_id = "task-fresh-room"
            isolation = {
                "schema_version": "schednav.agentteams-context-isolation/v2",
                "project_id": "fixture-project",
                "task_id": task_id,
                "worker_id": "scheduling-strategist",
                "worker_private_room_id": "fresh-private-room",
                "project_room_id": "project-room",
                "scope": "one-controller-one-observation",
                "observation_fingerprint": "a" * 64,
                "isolation_method": "fresh-private-room-single-assignment",
                "fresh_room_created_for_task": True,
                "fresh_room_verified_at": "2026-08-13T00:00:02Z",
                "assignment_dispatched_at": "2026-08-13T00:00:01Z",
                "assignment_context_verified": True,
                "assignment_context_evidence": (
                    "fresh-private-room-single-assignment"
                ),
                "cross_window_context_visible": False,
            }
            isolation["receipt_fingerprint"] = canonical_sha256(isolation)
            (task_dir / "context-isolation.json").write_text(
                json.dumps(isolation) + "\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "before fresh-room verification"):
                _verify_context_isolation(
                    task_dir,
                    meta={"room_id": "fresh-private-room"},
                    project_id="fixture-project",
                    task_id=task_id,
                    worker_id="scheduling-strategist",
                    observation_fingerprint="a" * 64,
                )


if __name__ == "__main__":
    unittest.main()
