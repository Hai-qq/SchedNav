from __future__ import annotations

import json
from pathlib import Path
import unittest

from schednav.contracts import canonical_sha256


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PublicEvidenceTests(unittest.TestCase):
    def test_rolling_receipt_preserves_the_negative_multi_agent_result(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "rolling-v1"
            / "alibaba-gpu-series-2-rolling-ablation-v1.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("evidence_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(
            receipt["schema_version"], "schednav.rolling-ablation-evidence/v1"
        )
        self.assertEqual(receipt["window_count"], 5)
        self.assertEqual(receipt["record_count"], 30)
        self.assertEqual(receipt["agentteams"]["plan_count"], 10)
        self.assertEqual(
            receipt["agentteams"]["model_id"], "deepseek-v4-flash"
        )
        self.assertEqual(
            receipt["arms"]["ordinary-fifo"]["hard_slo_pass_count"], 5
        )
        self.assertEqual(
            receipt["arms"]["rolling-multi-agent"]["hard_slo_pass_count"], 1
        )
        self.assertEqual(
            receipt["arms"]["rolling-single-agent"]["mean_metrics"],
            receipt["arms"]["rolling-multi-agent"]["mean_metrics"],
        )
        self.assertEqual(
            receipt["arms"]["rolling-workload-rule"]["mean_metrics"],
            receipt["arms"]["rolling-multi-agent"]["mean_metrics"],
        )
        self.assertEqual(
            receipt["multi_agent_superiority_gate"], "not_established"
        )
        self.assertEqual(
            receipt["multi_agent_vs_ordinary_gate"], "not_established"
        )

    def test_rolling_agentteams_closeout_keeps_human_approval_pending(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "rolling-v1"
            / "alibaba-gpu-series-2-rolling-agentteams-closeout-v1.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(
            receipt["schema_version"],
            "schednav.rolling-agentteams-closeout/v1",
        )
        self.assertEqual(receipt["model_id"], "deepseek-v4-flash")
        self.assertEqual(receipt["audit"]["record_fingerprint_verified_count"], 30)
        self.assertEqual(
            receipt["audit"]["deterministic_repetition_verified_count"], 30
        )
        self.assertEqual(receipt["audit"]["rolling_boundary_verified_count"], 15)
        self.assertEqual(
            receipt["decision"]["eligible_deployable_arm_ids"],
            ["ordinary-fifo"],
        )
        self.assertEqual(
            receipt["decision"]["recommended_arm_id"], "ordinary-fifo"
        )
        self.assertEqual(
            receipt["decision"]["scheduling_superiority_claim"],
            "not_established",
        )
        self.assertEqual(receipt["human_approval"]["status"], "approval_pending")
        self.assertFalse(receipt["human_approval"]["production_change_applied"])

    def test_rolling_v2_receipt_preserves_the_fifo_tie_and_real_call_counts(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "rolling-v2"
            / "alibaba-gpu-series-2-rolling-ablation-v2.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("evidence_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(receipt["study_id"], "rolling-ablation-v2")
        self.assertEqual(receipt["record_count"], 30)
        self.assertEqual(receipt["window_count"], 5)
        self.assertEqual(receipt["agentteams"]["plan_count"], 10)
        self.assertEqual(receipt["agentteams"]["model_id"], "deepseek-v4-flash")
        self.assertEqual(
            {
                arm_id: value["hard_slo_pass_count"]
                for arm_id, value in receipt["arms"].items()
            },
            {
                "fixed-tenant-predictive": 2,
                "ordinary-fifo": 4,
                "posthoc-catalog-oracle": 4,
                "rolling-multi-agent": 4,
                "rolling-single-agent": 4,
                "rolling-workload-rule": 4,
            },
        )
        fifo_metrics = receipt["arms"]["ordinary-fifo"]["mean_metrics"]
        for arm_id in (
            "posthoc-catalog-oracle",
            "rolling-multi-agent",
            "rolling-single-agent",
            "rolling-workload-rule",
        ):
            self.assertEqual(receipt["arms"][arm_id]["mean_metrics"], fifo_metrics)
        self.assertEqual(
            receipt["multi_agent_pairwise_hierarchy"],
            {
                "fixed-tenant-predictive": "better",
                "ordinary-fifo": "tie",
                "rolling-single-agent": "tie",
                "rolling-workload-rule": "tie",
            },
        )
        self.assertEqual(
            receipt["arms"]["rolling-single-agent"]["llm_call_count"], 31
        )
        self.assertEqual(
            receipt["arms"]["rolling-multi-agent"]["llm_call_count"], 61
        )
        self.assertEqual(receipt["multi_agent_superiority_gate"], "not_established")
        self.assertEqual(receipt["multi_agent_vs_ordinary_gate"], "not_established")

    def test_rolling_v2_closeout_recommends_no_ineligible_arm(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "rolling-v2"
            / "alibaba-gpu-series-2-rolling-agentteams-closeout-v2.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(receipt["project_id"], "proj-20260811-042605")
        self.assertEqual(receipt["model_id"], "deepseek-v4-flash")
        self.assertEqual(receipt["audit"]["record_fingerprint_verified_count"], 30)
        self.assertEqual(
            receipt["audit"]["deterministic_repetition_verified_count"], 30
        )
        self.assertEqual(receipt["audit"]["rolling_boundary_verified_count"], 15)
        self.assertEqual(receipt["decision"]["eligible_deployable_arm_ids"], [])
        self.assertIsNone(receipt["decision"]["recommended_arm_id"])
        self.assertEqual(
            receipt["decision"]["scheduling_superiority_claim"],
            "not_established",
        )
        self.assertEqual(receipt["human_approval"]["status"], "approval_pending")
        self.assertFalse(receipt["human_approval"]["production_change_applied"])

    def test_rolling_v3_receipt_preserves_the_negative_matched_handoff_result(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "rolling-v3"
            / "alibaba-gpu-series-2-rolling-ablation-v3.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("evidence_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(receipt["study_id"], "rolling-ablation-v3")
        self.assertEqual(receipt["record_count"], 35)
        self.assertEqual(receipt["window_count"], 5)
        self.assertEqual(receipt["agentteams"]["plan_count"], 15)
        self.assertEqual(receipt["agentteams"]["model_id"], "deepseek-v4-flash")
        self.assertEqual(
            {
                arm_id: value["hard_slo_pass_count"]
                for arm_id, value in receipt["arms"].items()
            },
            {
                "fixed-tenant-predictive": 2,
                "ordinary-fifo": 5,
                "posthoc-catalog-oracle": 5,
                "rolling-multi-agent": 3,
                "rolling-multi-agent-masked": 4,
                "rolling-single-agent": 3,
                "rolling-workload-rule": 4,
            },
        )
        self.assertEqual(
            receipt["arms"]["rolling-multi-agent"]["mean_metrics"],
            receipt["arms"]["rolling-single-agent"]["mean_metrics"],
        )
        self.assertEqual(receipt["arms"]["rolling-multi-agent"]["llm_call_count"], 120)
        self.assertEqual(
            receipt["arms"]["rolling-multi-agent-masked"]["llm_call_count"],
            120,
        )
        self.assertEqual(receipt["analyst_causal_pairwise_hierarchy"], "worse")
        self.assertEqual(receipt["analyst_causal_value_gate"], "not_established")
        self.assertEqual(receipt["multi_agent_superiority_gate"], "not_established")
        self.assertEqual(receipt["multi_agent_vs_ordinary_gate"], "not_established")

    def test_rolling_v3_closeout_recommends_only_the_all_window_fallback(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "rolling-v3"
            / "alibaba-gpu-series-2-rolling-agentteams-closeout-v3.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(receipt["project_id"], "proj-20260812-190350")
        self.assertEqual(receipt["model_id"], "deepseek-v4-flash")
        self.assertEqual(receipt["audit"]["record_fingerprint_verified_count"], 35)
        self.assertEqual(
            receipt["audit"]["deterministic_repetition_verified_count"], 35
        )
        self.assertEqual(receipt["audit"]["rolling_boundary_verified_count"], 20)
        self.assertEqual(
            receipt["decision"]["eligible_deployable_arm_ids"], ["ordinary-fifo"]
        )
        self.assertEqual(receipt["decision"]["recommended_arm_id"], "ordinary-fifo")
        self.assertEqual(
            receipt["decision"]["analyst_causal_value_claim"], "not_established"
        )
        self.assertEqual(
            receipt["decision"]["scheduling_superiority_claim"], "not_established"
        )
        self.assertEqual(receipt["human_approval"]["status"], "approval_pending")
        self.assertFalse(receipt["human_approval"]["production_change_applied"])

    def test_predictive_multiwindow_receipt_preserves_negative_holdout_result(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "predictive-v2"
            / "alibaba-gpu-series-2-predictive-multiwindow-v1.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(
            receipt["schema_version"],
            "schednav.predictive-multiwindow-evidence/v1",
        )
        self.assertEqual(receipt["status"], "no_calibration_eligible_arm")
        self.assertEqual(len(receipt["windows"]), 11)
        self.assertEqual(
            [item["partition"] for item in receipt["windows"]],
            ["calibration"] * 6 + ["holdout"] * 5,
        )
        self.assertFalse(
            receipt["information_boundary"]["controller_future_arrivals_visible"]
        )
        self.assertTrue(
            receipt["information_boundary"][
                "selection_lock_precedes_holdout_simulation"
            ]
        )
        self.assertEqual(
            receipt["calibration"]["selection"]["status"], "no_eligible_arm"
        )
        holdout = receipt["holdout"]["arms"]
        self.assertEqual(holdout["fifo"]["hard_slo_pass_count"], 5)
        self.assertEqual(holdout["guarded-static"]["hard_slo_pass_count"], 5)
        self.assertEqual(holdout["tenant-predictive"]["hard_slo_pass_count"], 1)
        self.assertEqual(holdout["aggregate-predictive"]["hard_slo_pass_count"], 0)
        self.assertLess(
            holdout["tenant-predictive"]["allocation_delta_vs_fifo"]["mean"], 0
        )
        self.assertGreater(
            holdout["tenant-predictive"]["allocation_rate_mean"]["mean"],
            holdout["aggregate-predictive"]["allocation_rate_mean"]["mean"],
        )

    def test_tenant_predictive_receipt_is_deterministic_and_preserves_rejection(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "predictive-v1"
            / "alibaba-gpu-series-2-2024-04-12-tenant-predictive.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(receipt["trace"]["schema_version"], "schednav.trace/v2")
        self.assertTrue(receipt["cutoff_forecast"]["deterministic_repeat"])
        self.assertTrue(receipt["closed_loop_replay"]["deterministic_repeat"])
        self.assertFalse(receipt["cutoff_forecast"]["future_demand_used"])
        self.assertEqual(receipt["slo_audit"]["hard_pass_count"], 7)
        self.assertEqual(
            receipt["slo_audit"]["failed_hard_constraints"],
            ["allocation-fifo-nondegradation"],
        )
        self.assertEqual(receipt["status"], "hard_slo_rejected")
        self.assertTrue(
            receipt["agentteams_bridge"]["forecast"]["output_matches_cli"]
        )
        self.assertTrue(
            receipt["agentteams_bridge"]["simulation"]["output_matches_cli"]
        )

    def test_representative_policy_receipt_is_content_addressed_and_approval_pending(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "native-v1"
            / "alibaba-gpu-series-2-2024-04-12-policy-evaluation.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(receipt["slo"]["hard_constraint_count"], 8)
        self.assertTrue(receipt["slo"]["all_candidates_passed_all_hard_constraints"])
        self.assertEqual(len(receipt["experiment"]["policies"]), 4)
        self.assertEqual(
            receipt["ranking"]["selection_status"],
            "tie_requires_human_approval",
        )
        self.assertEqual(len(receipt["ranking"]["remaining_action_ids"]), 3)

    def test_multiwindow_receipt_is_content_addressed_and_preserves_outcomes(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "native-v1"
            / "alibaba-gpu-series-2-multiwindow-30d-v1.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(receipt["schema_version"], "schednav.multiwindow-evidence/v1")
        self.assertEqual(receipt["study"]["eligible_window_count"], 112)
        self.assertEqual(receipt["study"]["selected_window_count"], 12)
        self.assertEqual(receipt["study"]["repetitions_per_policy_per_window"], 2)
        self.assertEqual(receipt["aggregate"]["window_count"], 12)
        self.assertEqual(
            receipt["aggregate"]["selection_status_counts"],
            {
                "no_eligible_policy": 1,
                "selected": 5,
                "tie_requires_human_approval": 6,
            },
        )
        uplift = receipt["aggregate"]["best_hard_pass_allocation_uplift_vs_fifo"]
        self.assertEqual(uplift["positive_window_count"], 5)
        self.assertEqual(uplift["equal_window_count"], 6)
        self.assertEqual(uplift["negative_window_count"], 0)
        self.assertFalse(receipt["aggregate"]["universal_winner_declared"])
        self.assertTrue(
            all(
                policy["deterministic_window_count"] == 12
                for policy in receipt["aggregate"]["policies"].values()
            )
        )
        self.assertEqual(
            receipt["study"]["action_space"]["excluded_profiles"][0]["action_id"],
            "native-preemptive-1800",
        )

    def test_v2_multiwindow_receipt_records_guarded_policy_frontier(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "native-v2"
            / "alibaba-gpu-series-2-multiwindow-30d-v2.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(receipt["study"]["selected_window_count"], 12)
        self.assertEqual(receipt["aggregate"]["window_count"], 12)
        self.assertEqual(
            receipt["aggregate"]["selection_status_counts"],
            {
                "no_eligible_policy": 1,
                "selected": 2,
                "tie_requires_human_approval": 9,
            },
        )
        uplift = receipt["aggregate"]["best_hard_pass_allocation_uplift_vs_fifo"]
        self.assertEqual(uplift["positive_window_count"], 7)
        self.assertEqual(uplift["equal_window_count"], 4)
        self.assertEqual(uplift["negative_window_count"], 0)
        self.assertEqual(receipt["study"]["action_space"]["excluded_profiles"], [])
        self.assertFalse(any("native-preemptive-1800" in item for item in receipt["limitations"]))

    def test_second_trace_receipt_is_compatibility_evidence(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "native-v2"
            / "alibaba-gpu-v2023-qos-full.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(receipt["interpretation"]["claim_scope"], "compatibility_only")
        self.assertEqual(receipt["workload"]["population"], {"HP": 3590, "Spot": 2510})
        self.assertLess(
            receipt["workload"]["regime_signals"]["combined_peak_active_pressure"],
            0.01,
        )
        self.assertTrue(receipt["experiment"]["portfolio_comparable"])
        self.assertTrue(receipt["experiment"]["all_candidates_passed_hard_slo"])
        self.assertEqual(receipt["ranking"]["selection_status"], "tie_requires_human_approval")

    def test_v3_all_window_receipt_covers_every_eligible_window(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "native-v3"
            / "alibaba-gpu-series-2-all112-v3.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(receipt["study"]["eligible_window_count"], 112)
        self.assertEqual(receipt["study"]["selected_window_count"], 112)
        self.assertEqual(receipt["study"]["repetitions_per_policy_per_window"], 2)
        self.assertEqual(receipt["aggregate"]["window_count"], 112)
        self.assertEqual(
            receipt["aggregate"]["selection_status_counts"],
            {
                "no_eligible_policy": 12,
                "selected": 35,
                "tie_requires_human_approval": 65,
            },
        )
        self.assertTrue(
            all(
                policy["deterministic_window_count"] == 112
                for policy in receipt["aggregate"]["policies"].values()
            )
        )

    def test_adaptive_holdout_receipt_records_value_and_candidate_cost(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "native-v3"
            / "alibaba-gpu-series-2-adaptive-holdout-v3.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(receipt["schema_version"], "schednav.adaptive-evidence/v1")
        self.assertEqual(receipt["study"]["split"]["calibration_window_count"], 67)
        self.assertEqual(receipt["study"]["split"]["evaluation_window_count"], 45)
        self.assertEqual(
            receipt["study"]["agentteams"]["model_id"], "deepseek-v4-flash"
        )
        self.assertEqual(receipt["best_static_action_id"], "native-fifo")
        agent = receipt["controllers"]["agentteams"]
        rule = receipt["controllers"]["workload_rule"]
        oracle = receipt["controllers"]["catalog_oracle"]
        self.assertEqual(oracle["catalog_oracle_feasible_window_count"], 41)
        self.assertEqual(agent["hard_slo_feasible_window_count"], 41)
        self.assertEqual(
            agent["catalog_oracle_frontier_coverage_window_count"], 41
        )
        self.assertEqual(
            agent["catalog_oracle_frontier_exact_match_window_count"], 27
        )
        self.assertEqual(agent["candidate_policy_evaluation_count"], 185)
        self.assertEqual(
            agent["candidate_policy_evaluation_reduction_vs_catalog_oracle"],
            0.177777778,
        )
        self.assertEqual(
            agent["selected_allocation_uplift_vs_fifo"][
                "lower_bound_positive_window_count"
            ],
            5,
        )
        self.assertEqual(
            agent["selected_allocation_uplift_vs_fifo"][
                "lower_bound_negative_window_count"
            ],
            0,
        )
        self.assertEqual(
            agent["candidate_set_catalog_best_allocation_coverage_window_count"],
            39,
        )
        self.assertEqual(rule["candidate_policy_evaluation_count"], 135)
        self.assertEqual(
            rule["catalog_oracle_frontier_coverage_window_count"], 39
        )
        self.assertEqual(
            rule["catalog_oracle_frontier_exact_match_window_count"], 19
        )
        self.assertEqual(
            rule["candidate_set_catalog_best_allocation_coverage_window_count"],
            33,
        )
        self.assertEqual(len(receipt["evaluation_windows"]), 45)


if __name__ == "__main__":
    unittest.main()
