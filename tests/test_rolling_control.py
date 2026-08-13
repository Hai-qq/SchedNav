from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from schednav.native_simulator import SimulationPolicy, build_metrics_report, simulate_trace
from schednav.native_trace import TraceJob, TraceNode, load_canonical_trace, write_canonical_trace
from schednav.rolling_control import (
    RecordedAgentCandidateProvider,
    RollingPolicyController,
    WorkloadRuleCandidateProvider,
    _decompose_forecast_hp_request,
    _scenario_trace,
    build_agent_plan,
)
from schednav.rolling_experiment import load_rolling_action_space


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _catalog() -> list[SimulationPolicy]:
    names = (
        "native-fifo.json",
        "native-preemptive-3600.json",
        "native-preemptive-g3600-b09-d0000.json",
        "native-preemptive-g3600-b09-d0900.json",
        "native-preemptive-g3600-b09-loss-aware.json",
    )
    return [
        SimulationPolicy.load(PROJECT_ROOT / "configs" / "policies" / name)
        for name in names
    ]


def _slo() -> dict:
    import json

    return json.loads(
        (PROJECT_ROOT / "configs" / "slos" / "schednav-demo-slo-v1.json").read_text(
            encoding="utf-8"
        )
    )


class RollingControlTests(unittest.TestCase):
    def _trace(self, root: Path, *, future_variant: str = "a"):
        jobs = []
        for index in range(6):
            start = index * 600
            jobs.extend(
                (
                    TraceJob(f"warm-hp-{index}", start, 180, 1, "HP", "A", "t1"),
                    TraceJob(
                        f"warm-spot-{index}", start + 180, 240, 1, "Spot", "A", "t2"
                    ),
                )
            )
        jobs.extend(
            (
                TraceJob("eval-spot-long", 3600, 5400, 2, "Spot", "A", "t2"),
                TraceJob("eval-hp-1", 3900, 600, 2, "HP", "A", "t1"),
                TraceJob("eval-spot-2", 7500, 900, 2, "Spot", "A", "t2"),
                TraceJob(
                    f"eval-hp-{future_variant}",
                    7800,
                    600 if future_variant == "a" else 1800,
                    2,
                    "HP",
                    "A",
                    "t1",
                ),
            )
        )
        return load_canonical_trace(
            write_canonical_trace(
                root,
                trace_id="rolling-small",
                time_origin="2026-01-01 00:00:00",
                source={"dataset": "rolling-unit-fixture"},
                nodes=[TraceNode("n1", "A", 4)],
                jobs=jobs,
                evaluation_start_seconds=3600,
                evaluation_end_seconds=10800,
                schema_version="schednav.trace/v2",
            )
        )

    def _controller(self, trace):
        return RollingPolicyController(
            controller_id="rolling-rule-test",
            mode="workload_rule",
            trace=trace,
            policies=_catalog(),
            slo=_slo(),
            candidate_provider=WorkloadRuleCandidateProvider(),
            decision_interval_seconds=3600,
            scenario_horizon_seconds=3600,
            history_window_seconds=3600,
            candidate_budget=3,
        )

    def test_single_session_preserves_state_and_consumes_equal_budget(self):
        with TemporaryDirectory() as temporary:
            trace = self._trace(Path(temporary))
            first = simulate_trace(trace, _catalog()[0], rolling_controller=self._controller(trace))
            second = simulate_trace(trace, _catalog()[0], rolling_controller=self._controller(trace))

        self.assertEqual(first["result_fingerprint"], second["result_fingerprint"])
        report = first["rolling_control"]
        self.assertEqual(report["decision_count"], 2)
        self.assertEqual(report["candidate_simulation_count"], 6)
        self.assertTrue(report["state_handoff"]["single_simulator_session"])
        self.assertFalse(report["state_handoff"]["state_reinitialized_between_cutoffs"])
        self.assertFalse(
            report["information_boundary"]["agent_future_arrivals_visible"]
        )
        self.assertTrue(
            all(
                decision["real_future_execution_frozen_after_selection"]
                for decision in report["decisions"]
            )
        )
        self.assertEqual(
            report["decisions"][1]["previous_decision_fingerprint"],
            report["decisions"][0]["decision_fingerprint"],
        )
        metrics = build_metrics_report(first)
        self.assertEqual(metrics["jobs"]["HP"]["completion_rate"], 1.0)
        self.assertEqual(metrics["jobs"]["Spot"]["completion_rate"], 1.0)

    def test_future_arrival_changes_do_not_change_first_cutoff_selection(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_trace = self._trace(root / "first", future_variant="a")
            second_trace = self._trace(root / "second", future_variant="b")
            first = simulate_trace(
                first_trace,
                _catalog()[0],
                rolling_controller=self._controller(first_trace),
            )
            second = simulate_trace(
                second_trace,
                _catalog()[0],
                rolling_controller=self._controller(second_trace),
            )

        left = first["rolling_control"]["decisions"][0]
        right = second["rolling_control"]["decisions"][0]
        self.assertEqual(left["observation_fingerprint"], right["observation_fingerprint"])
        self.assertEqual(
            left["candidate_selection"]["selection_fingerprint"],
            right["candidate_selection"]["selection_fingerprint"],
        )
        self.assertEqual(left["selected_action_id"], right["selected_action_id"])

    def test_recorded_agent_plan_is_model_and_observation_bound(self):
        observation = {
            "observation_fingerprint": "a" * 64,
        }
        decision = {
            "observation_fingerprint": "a" * 64,
            "candidate_action_ids": [
                "native-fifo",
                "native-preemptive-g3600-b09-d0000",
                "native-preemptive-g3600-b09-loss-aware",
            ],
            "reason_code": "bounded-test",
            "agent_stage_receipts": [
                {
                    "role": "Scheduling Strategist",
                    "task_fingerprint": "b" * 64,
                }
            ],
            "llm_call_count": 1,
            "prompt_tokens": 100,
            "completion_tokens": 20,
        }
        plan = build_agent_plan(
            controller_id="single-agent-test",
            mode="single_agent",
            decisions=[decision],
            source_project_id="agentteams-unit-test",
        )
        provider = RecordedAgentCandidateProvider(plan)
        selection = provider.select_candidates(
            observation,
            [{"action_id": policy.action_id} for policy in _catalog()],
            3,
        )
        self.assertEqual(selection["model_id"], "deepseek-v4-flash")
        self.assertEqual(selection["llm_call_count"], 1)
        with self.assertRaisesRegex(ValueError, "no decision"):
            provider.select_candidates(
                {"observation_fingerprint": "c" * 64},
                [{"action_id": policy.action_id} for policy in _catalog()],
                3,
            )

    def test_v2_action_space_declares_fifo_equivalent_safety_baseline(self):
        policies, baseline_action_id = load_rolling_action_space(
            PROJECT_ROOT,
            PROJECT_ROOT / "configs/action_spaces/rolling-predictive-v2.json",
        )
        self.assertEqual(baseline_action_id, "rolling-fifo-open")
        baseline = next(
            policy for policy in policies if policy.action_id == baseline_action_id
        )
        self.assertEqual(baseline.scheduler, "fifo")
        self.assertEqual(baseline.predictive_admission_mode, "bypass")
        selected = WorkloadRuleCandidateProvider(
            baseline_action_id
        ).select_candidates(
            {
                "observation_fingerprint": "d" * 64,
                "workload_signals": {
                    "hp_peak_active_pressure": 0.2,
                    "spot_requested_gpu_share": 0.7,
                    "hp_recent_to_prior_requested_gpu_ratio": 1.0,
                },
                "scheduler_state": {"queue": {"hp_job_count": 0}},
            },
            [
                {"action_id": policy.action_id}
                for policy in policies
            ],
            3,
        )["candidate_action_ids"]
        self.assertEqual(selected.count(baseline_action_id), 1)

    def test_v2_agent_plan_is_bound_to_declared_safety_baseline(self):
        decisions = [
            {
                "observation_fingerprint": "e" * 64,
                "candidate_action_ids": [
                    "rolling-fifo-open",
                    "rolling-preemptive-open-d0000",
                    "rolling-preemptive-open-loss-aware",
                ],
                "reason_code": "bounded-v2-test",
                "agent_stage_receipts": [
                    {
                        "role": "Scheduling Strategist",
                        "task_fingerprint": "f" * 64,
                    }
                ],
                "llm_call_count": 1,
                "prompt_tokens": 100,
                "completion_tokens": 20,
            }
        ]
        plan = build_agent_plan(
            controller_id="single-agent-v2-test",
            mode="single_agent",
            decisions=decisions,
            source_project_id="agentteams-unit-test",
            baseline_action_id="rolling-fifo-open",
        )
        self.assertEqual(
            plan["required_candidate_action_id"], "rolling-fifo-open"
        )
        policies, _baseline = load_rolling_action_space(
            PROJECT_ROOT,
            PROJECT_ROOT / "configs/action_spaces/rolling-predictive-v2.json",
        )
        provider = RecordedAgentCandidateProvider(plan, "rolling-fifo-open")
        selection = provider.select_candidates(
            {"observation_fingerprint": "e" * 64},
            [{"action_id": policy.action_id} for policy in policies],
            3,
        )
        self.assertIn("rolling-fifo-open", selection["candidate_action_ids"])
        with self.assertRaisesRegex(ValueError, "another safety baseline"):
            RecordedAgentCandidateProvider(plan, "native-fifo")

    def test_forecast_aggregate_is_split_into_cutoff_visible_job_shapes(self):
        templates = [
            {"gpu_count": 2, "tenant_id": "t1", "source_job_id": "h1"},
            {"gpu_count": 4, "tenant_id": "t2", "source_job_id": "h2"},
        ]
        pieces = _decompose_forecast_hp_request(
            16,
            templates,
            capacity_gpus=16,
            point_index=0,
        )
        self.assertAlmostEqual(sum(item["gpu_count"] for item in pieces), 16)
        self.assertGreater(len(pieces), 1)
        self.assertLessEqual(max(item["gpu_count"] for item in pieces), 4)
        self.assertEqual({item["tenant_id"] for item in pieces}, {"t1", "t2"})

    def test_past_only_scenario_does_not_turn_forecast_into_one_gang_job(self):
        with TemporaryDirectory() as temporary:
            trace = self._trace(Path(temporary))
            scenario = _scenario_trace(
                trace,
                trace,
                3600,
                3600,
                {"carryover_jobs": [], "snapshot_fingerprint": "a" * 64},
                {
                    "projection_fingerprint": "b" * 64,
                    "forecast_points": [
                        {
                            "target_time_seconds": 7200,
                            "horizon_step": 1,
                            "resource_pool": "A",
                            "guarantee_quantile_gpus": 4,
                        }
                    ],
                },
                use_actual_future=False,
            )

        forecast_jobs = [
            job for job in scenario.jobs if job.job_id.startswith("forecast-hp::")
        ]
        self.assertEqual(scenario.source["generator"], "schednav.past-replay-scenario/v3")
        self.assertFalse(scenario.source["future_arrivals_visible"])
        self.assertAlmostEqual(sum(job.gpu_count for job in forecast_jobs), 4)
        self.assertGreater(len(forecast_jobs), 1)
        self.assertLess(max(job.gpu_count for job in forecast_jobs), 4)
        decomposition = scenario.source["forecast_hp_decomposition"]
        self.assertTrue(decomposition["aggregate_demand_preserved"])
        self.assertFalse(decomposition["future_job_shapes_visible"])

    def test_forecast_total_subtracts_visible_surviving_hp_carryover(self):
        with TemporaryDirectory() as temporary:
            trace = self._trace(Path(temporary))
            scenario = _scenario_trace(
                trace,
                trace,
                3600,
                3600,
                {
                    "carryover_jobs": [
                        {
                            "job_id": "running-hp",
                            "status": "running",
                            "service_class": "HP",
                            "gpu_model": "A",
                            "gpu_count": 2,
                            "tenant_id": "t1",
                            "current_run_elapsed_seconds": 0,
                            "current_queue_wait_seconds": 0,
                        }
                    ],
                    "snapshot_fingerprint": "a" * 64,
                },
                {
                    "projection_fingerprint": "b" * 64,
                    "forecast_points": [
                        {
                            "target_time_seconds": 3660,
                            "horizon_step": 1,
                            "resource_pool": "A",
                            "guarantee_quantile_gpus": 4,
                        }
                    ],
                },
                use_actual_future=False,
            )

        forecast_jobs = [
            job for job in scenario.jobs if job.job_id.startswith("forecast-hp::")
        ]
        self.assertAlmostEqual(sum(job.gpu_count for job in forecast_jobs), 2)
        point = scenario.source["forecast_hp_decomposition"]["points"][0]
        self.assertEqual(point["forecast_total_active_gpus"], 4)
        self.assertEqual(point["surviving_carryover_hp_gpus"], 2)
        self.assertEqual(point["scenario_incremental_hp_gpus"], 2)


if __name__ == "__main__":
    unittest.main()
