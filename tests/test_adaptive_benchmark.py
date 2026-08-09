from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import unittest

from schednav.adaptive_benchmark import (
    build_adaptive_design,
    build_adaptive_evidence,
    build_rule_controller,
    evaluate_adaptive_benchmark,
)
from schednav.contracts import canonical_sha256


ACTION_IDS = [
    "native-fifo",
    "native-preemptive-3600",
    "native-preemptive-g3600-b09-d0000",
    "native-preemptive-g3600-b09-d0900",
    "native-preemptive-g3600-b09-loss-aware",
]


def _selection() -> dict:
    windows = []
    for index in range(4):
        windows.append(
            {
                "window_seconds": {
                    "start": float(index * 86400),
                    "end": float((index + 1) * 86400 - 1),
                },
                "population": {
                    "HP": {"job_count": 20, "requested_gpus": 30 + index},
                    "Spot": {"job_count": 20, "requested_gpus": 20 + index},
                },
                "spot_requested_gpu_share": 0.4 + index * 0.05,
                "combined_peak_active_pressure": 0.7 + index * 0.1,
                "combined_mean_active_pressure": 0.5 + index * 0.1,
                "stratum": {"mode": "all_eligible", "ordinal": index + 1},
            }
        )
    value = {
        "schema_version": "schednav.multiwindow-selection/v1",
        "trace_id": "adaptive-fixture",
        "trace_fingerprint": "a" * 64,
        "source": {"dataset": "unit-fixture"},
        "capacity_gpus": 100,
        "eligibility": {},
        "selection_method": {
            "name": "all-eligible-origin-aligned",
            "selected_before_simulation": True,
        },
        "eligible_window_count": 4,
        "eligible_windows_fingerprint": "b" * 64,
        "selected_window_count": 4,
        "selected_windows": windows,
        "definition": "fixture",
    }
    value["selection_fingerprint"] = canonical_sha256(value)
    return value


def _policies() -> list[dict]:
    return [
        {
            "schema_version": "schednav.simulation-policy/v1",
            "action_id": action_id,
            "scheduler": "fifo" if action_id == "native-fifo" else "priority_preemptive",
            "spot_guarantee_seconds": 3600,
            "checkpoint_interval_seconds": 3600,
            "preemption_overhead_seconds": 80,
            "placement_strategy": "deterministic_best_fit",
        }
        for action_id in ACTION_IDS
    ]


def _summary(design: dict) -> dict:
    allocations = {
        "native-fifo": [0.70, 0.70, 0.75, 0.76],
        "native-preemptive-3600": [0.90, 0.90, 0.90, 0.90],
        "native-preemptive-g3600-b09-d0000": [0.75, 0.76, 0.82, 0.83],
        "native-preemptive-g3600-b09-d0900": [0.74, 0.74, 0.81, 0.82],
        "native-preemptive-g3600-b09-loss-aware": [0.73, 0.73, 0.85, 0.84],
    }
    records = []
    for index, window in enumerate(design["windows"]):
        policies = []
        for action_id in ACTION_IDS:
            hard_pass = not (
                action_id == "native-preemptive-3600" and index in {0, 2, 3}
            )
            policies.append(
                {
                    "action_id": action_id,
                    "hard_slo_passed": hard_pass,
                    "allocation_rate_mean": allocations[action_id][index],
                    "spot_jct_p95_seconds": 100 - allocations[action_id][index],
                    "spot_eviction_rate_per_run": 0.01,
                }
            )
        records.append(
            {
                "window_id": window["window_id"],
                "date": window["date"],
                "policies": policies,
            }
        )
    value = {
        "schema_version": "schednav.multiwindow-summary/v1",
        "selection_fingerprint": design["selection_fingerprint"],
        "windows": records,
    }
    value["multiwindow_fingerprint"] = canonical_sha256(value)
    return value


class AdaptiveBenchmarkTests(unittest.TestCase):
    def setUp(self):
        action_space = {
            "schema_version": "schednav.native-action-space/v1",
            "name": "fixture-v3",
            "profiles": [f"configs/{action}.json" for action in ACTION_IDS],
        }
        self.design = build_adaptive_design(
            _selection(),
            action_space,
            _policies(),
            action_space_path="configs/action_spaces/fixture.json",
            time_origin="2026-01-01 00:00:00",
            gpu_model="A",
            calibration_fraction=0.5,
        )
        self.rule = build_rule_controller(self.design)
        self.agent = deepcopy(self.rule)
        self.agent["controller_id"] = "agentteams-fixture"
        self.agent["model_id"] = "deepseek-v4-flash"
        self.agent["definition"] = "fixture AgentTeams selection"
        self.agent.pop("controller_fingerprint")
        self.agent["controller_fingerprint"] = canonical_sha256(self.agent)

    def test_design_freezes_chronological_holdout_before_simulation(self):
        self.assertEqual(self.design["split"]["calibration_window_count"], 2)
        self.assertEqual(self.design["split"]["evaluation_window_count"], 2)
        self.assertEqual(
            [window["partition"] for window in self.design["windows"]],
            ["calibration", "calibration", "evaluation", "evaluation"],
        )
        self.assertRegex(self.design["design_fingerprint"], "^[0-9a-f]{64}$")

    def test_evaluates_four_controllers_against_catalog_oracle(self):
        report = evaluate_adaptive_benchmark(
            _summary(self.design), self.design, self.rule, self.agent
        )

        self.assertEqual(
            report["best_static_action_id"],
            "native-preemptive-g3600-b09-d0000",
        )
        self.assertEqual(
            set(report["controllers"]),
            {"fifo", "best_static", "workload_rule", "agentteams", "catalog_oracle"},
        )
        self.assertEqual(
            report["controllers"]["catalog_oracle"][
                "catalog_oracle_frontier_coverage_window_count"
            ],
            2,
        )
        self.assertEqual(
            report["controllers"]["catalog_oracle"][
                "candidate_policy_evaluation_reduction_vs_catalog_oracle"
            ],
            0.0,
        )
        self.assertGreater(
            report["controllers"]["agentteams"][
                "candidate_policy_evaluation_reduction_vs_catalog_oracle"
            ],
            0.0,
        )
        self.assertRegex(report["benchmark_fingerprint"], "^[0-9a-f]{64}$")

    def test_reports_formal_hierarchy_separately_from_raw_allocation_maximum(self):
        summary = _summary(self.design)
        evaluation = summary["windows"][2]
        policies = {item["action_id"]: item for item in evaluation["policies"]}
        policies["native-preemptive-g3600-b09-d0000"].update(
            allocation_rate_mean=0.82,
            spot_jct_p95_seconds=10.0,
        )
        policies["native-preemptive-g3600-b09-loss-aware"].update(
            allocation_rate_mean=0.825,
            spot_jct_p95_seconds=20.0,
        )
        summary.pop("multiwindow_fingerprint")
        summary["multiwindow_fingerprint"] = canonical_sha256(summary)

        report = evaluate_adaptive_benchmark(
            summary, self.design, self.rule, self.agent
        )
        window = report["controllers"]["agentteams"]["windows"][0]

        self.assertEqual(
            window["selected_action_ids"],
            ["native-preemptive-g3600-b09-d0000"],
        )
        self.assertEqual(window["selected_allocation_rate_range"]["minimum"], 0.82)
        self.assertEqual(
            window["candidate_set_best_hard_pass_allocation_rate"], 0.825
        )
        self.assertTrue(window["catalog_oracle_frontier_covered"])
        self.assertTrue(window["catalog_oracle_frontier_exact_match"])

    def test_rejects_an_agent_controller_from_another_model(self):
        invalid = deepcopy(self.agent)
        invalid["model_id"] = "another-model"
        invalid.pop("controller_fingerprint")
        invalid["controller_fingerprint"] = canonical_sha256(invalid)
        with self.assertRaisesRegex(ValueError, "deepseek-v4-flash"):
            evaluate_adaptive_benchmark(
                _summary(self.design), self.design, self.rule, invalid
            )

    def test_builds_compact_content_addressed_public_evidence(self):
        benchmark = evaluate_adaptive_benchmark(
            _summary(self.design), self.design, self.rule, self.agent
        )
        receipt = build_adaptive_evidence(self.design, benchmark, self.agent)

        self.assertEqual(receipt["schema_version"], "schednav.adaptive-evidence/v1")
        self.assertEqual(len(receipt["evaluation_windows"]), 2)
        self.assertNotIn("windows", receipt["controllers"]["agentteams"])
        self.assertRegex(receipt["receipt_fingerprint"], "^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
