from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from schednav.native_simulator import SimulationPolicy, build_metrics_report, simulate_trace
from schednav.native_trace import TraceJob, TraceNode, load_canonical_trace, write_canonical_trace
from schednav.policy_portfolio import compare_policy_portfolio
from schednav.slo import audit_slo


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _policy(scheduler: str, guarantee: int = 0) -> SimulationPolicy:
    return SimulationPolicy.from_dict(
        {
            "schema_version": "schednav.simulation-policy/v1",
            "action_id": f"test-{scheduler}-{guarantee}",
            "scheduler": scheduler,
            "spot_guarantee_seconds": guarantee,
            "checkpoint_interval_seconds": 2,
            "preemption_overhead_seconds": 0,
            "placement_strategy": "deterministic_best_fit",
        }
    )


def _guarded_policy(*, delay: int = 0, budget: float = 0.1) -> SimulationPolicy:
    value = _policy("priority_preemptive").to_dict()
    value["schema_version"] = "schednav.simulation-policy/v1"
    value["action_id"] = f"guarded-{delay}-{budget}"
    value["hp_preemption_delay_seconds"] = delay
    value["spot_eviction_budget_rate"] = budget
    return SimulationPolicy.from_dict(value)


class NativeSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        manifest = write_canonical_trace(
            root,
            trace_id="preemption-small",
            time_origin="2026-01-01 00:00:00",
            source={"dataset": "unit-fixture"},
            nodes=[TraceNode("n1", "A", 2)],
            jobs=[
                TraceJob("spot", 0, 10, 2, "Spot", "A"),
                TraceJob("hp", 2, 2, 2, "HP", "A"),
            ],
        )
        self.trace = load_canonical_trace(manifest)

    def tearDown(self):
        self.temporary.cleanup()

    def test_fifo_preserves_arrival_order_without_preemption(self):
        result = simulate_trace(self.trace, _policy("fifo"))
        jobs = {job["job_id"]: job for job in result["jobs"]}
        self.assertEqual(jobs["spot"]["completion_time_seconds"], 10)
        self.assertEqual(jobs["hp"]["start_time_seconds"], 10)
        self.assertEqual(jobs["hp"]["queue_seconds"], 8)
        self.assertEqual(result["preemption_events"], [])
        self.assertEqual(result["cluster"]["allocation_rate_mean"], 1.0)

    def test_priority_policy_preempts_spot_and_emits_canonical_metrics(self):
        result = simulate_trace(self.trace, _policy("priority_preemptive"))
        repeated = simulate_trace(self.trace, _policy("priority_preemptive"))
        self.assertEqual(result["result_fingerprint"], repeated["result_fingerprint"])
        jobs = {job["job_id"]: job for job in result["jobs"]}
        self.assertEqual(jobs["hp"]["start_time_seconds"], 2)
        self.assertEqual(jobs["hp"]["queue_seconds"], 0)
        self.assertEqual(jobs["spot"]["preemption_count"], 1)
        self.assertEqual(jobs["spot"]["run_count"], 2)

        metrics = build_metrics_report(result)
        self.assertEqual(metrics["schema_version"], "schednav.metrics-report/v2")
        self.assertEqual(metrics["source"]["engine"]["name"], "schednav-sim")
        self.assertEqual(metrics["preemption_events"]["eviction_rate_per_run"], 0.5)
        self.assertTrue(metrics["preemption_events"]["consistent_with_job_csv"])
        self.assertTrue(metrics["spot_runs"]["consistent_with_job_csv"])
        self.assertEqual(metrics["spot_guarantee"]["success_rate"], 1.0)

    def test_guarantee_delays_preemption_until_the_declared_boundary(self):
        result = simulate_trace(self.trace, _policy("priority_preemptive", guarantee=5))
        event = result["preemption_events"][0]
        jobs = {job["job_id"]: job for job in result["jobs"]}
        self.assertEqual(event["time_seconds"], 5)
        self.assertEqual(event["rollback_seconds"], 1)
        self.assertEqual(jobs["hp"]["queue_seconds"], 3)

    def test_hp_preemption_delay_waits_before_eviction(self):
        result = simulate_trace(self.trace, _guarded_policy(delay=3, budget=1.0))
        event = result["preemption_events"][0]
        jobs = {job["job_id"]: job for job in result["jobs"]}
        self.assertEqual(event["time_seconds"], 5)
        self.assertEqual(jobs["hp"]["queue_seconds"], 3)

    def test_eviction_budget_blocks_a_projected_rate_above_the_cap(self):
        blocked = simulate_trace(self.trace, _guarded_policy(budget=0.49))
        allowed = simulate_trace(self.trace, _guarded_policy(budget=0.50))
        blocked_jobs = {job["job_id"]: job for job in blocked["jobs"]}
        self.assertEqual(blocked["preemption_events"], [])
        self.assertEqual(blocked_jobs["hp"]["queue_seconds"], 8)
        self.assertEqual(len(allowed["preemption_events"]), 1)

    def test_warmup_runs_cannot_dilute_the_evaluation_eviction_budget(self):
        root = Path(self.temporary.name)
        warmup = [
            TraceJob(f"warmup-{index}", index * 2, 2, 2, "Spot", "A")
            for index in range(10)
        ]
        trace = load_canonical_trace(
            write_canonical_trace(
                root / "budget-window",
                trace_id="budget-window",
                time_origin="2026-01-01 00:00:00",
                source={"dataset": "unit-fixture"},
                nodes=[TraceNode("n1", "A", 2)],
                jobs=[
                    *warmup,
                    TraceJob("evaluated-spot", 20, 10, 2, "Spot", "A"),
                    TraceJob("evaluated-hp", 22, 2, 2, "HP", "A"),
                ],
                evaluation_start_seconds=20,
                evaluation_end_seconds=22,
            )
        )

        result = simulate_trace(trace, _guarded_policy(budget=0.1))
        jobs = {job["job_id"]: job for job in result["jobs"]}

        self.assertEqual(result["preemption_events"], [])
        self.assertEqual(jobs["evaluated-hp"]["queue_seconds"], 8)

    def test_loss_aware_victim_strategy_minimizes_checkpoint_rollback(self):
        root = Path(self.temporary.name)
        trace = load_canonical_trace(
            write_canonical_trace(
                root / "victim-strategy",
                trace_id="victim-strategy",
                time_origin="2026-01-01 00:00:00",
                source={"dataset": "unit-fixture"},
                nodes=[TraceNode("n1", "A", 2)],
                jobs=[
                    TraceJob("spot-long", 0, 30, 1, "Spot", "A"),
                    TraceJob("spot-low-loss", 1, 10, 1, "Spot", "A"),
                    TraceJob("hp", 5, 1, 1, "HP", "A"),
                ],
            )
        )
        longest = _policy("priority_preemptive").to_dict()
        longest["schema_version"] = "schednav.simulation-policy/v1"
        longest["action_id"] = "longest"
        loss_aware = dict(longest)
        loss_aware["action_id"] = "loss-aware"
        loss_aware["preemption_victim_strategy"] = "lowest_checkpoint_loss"

        longest_result = simulate_trace(trace, SimulationPolicy.from_dict(longest))
        loss_aware_result = simulate_trace(
            trace, SimulationPolicy.from_dict(loss_aware)
        )

        self.assertEqual(
            longest_result["preemption_events"][0]["preempted_job_id"],
            "spot-long",
        )
        self.assertEqual(
            loss_aware_result["preemption_events"][0]["preempted_job_id"],
            "spot-low-loss",
        )
        self.assertGreater(
            longest_result["preemption_events"][0]["rollback_seconds"],
            loss_aware_result["preemption_events"][0]["rollback_seconds"],
        )

    def test_default_optional_controls_preserve_v1_policy_evidence(self):
        policy = _policy("priority_preemptive")
        self.assertNotIn("hp_preemption_delay_seconds", policy.to_dict())
        self.assertNotIn("spot_eviction_budget_rate", policy.to_dict())
        self.assertNotIn("preemption_victim_strategy", policy.to_dict())
        self.assertEqual(
            policy.fingerprint,
            "d5d4baa5a10c6e0c22b0d88ccd474f976c3c89546492e35dbbba3910fc757880",
        )

    def test_native_metrics_flow_through_portfolio_and_slo_audit(self):
        root = Path(self.temporary.name)
        metric_paths = []
        for name, policy in (
            ("fifo", _policy("fifo")),
            ("immediate", _policy("priority_preemptive")),
            ("guaranteed", _policy("priority_preemptive", guarantee=5)),
        ):
            metrics = build_metrics_report(simulate_trace(self.trace, policy))
            path = root / f"{name}-metrics.json"
            path.write_text(json.dumps(metrics), encoding="utf-8")
            metric_paths.append(path)
        portfolio = compare_policy_portfolio(metric_paths)
        self.assertTrue(portfolio["comparable"], portfolio["criteria"])
        self.assertTrue(portfolio["criteria"]["metrics_schema_supported"])

        slo = {
            "schema_version": "schednav.slo-spec/v1",
            "name": "native-test",
            "constraints": [
                {
                    "id": "hp-complete",
                    "metric": "hp_completion_rate",
                    "operator": ">=",
                    "threshold": 1.0,
                    "severity": "hard",
                },
                {
                    "id": "spot-complete",
                    "metric": "spot_completion_rate",
                    "operator": ">=",
                    "threshold": 1.0,
                    "severity": "hard",
                },
            ],
            "ranking": [],
        }
        slo_path = root / "slo.json"
        slo_path.write_text(json.dumps(slo), encoding="utf-8")
        audit = audit_slo(metric_paths[1], slo_path)
        self.assertTrue(audit["metrics_schema_supported"])
        self.assertTrue(audit["audit_passed"])

    def test_explicit_window_replays_warmup_but_audits_only_window_arrivals(self):
        root = Path(self.temporary.name)
        trace = load_canonical_trace(
            write_canonical_trace(
                root / "windowed",
                trace_id="windowed",
                time_origin="2026-01-01 00:00:00",
                source={"dataset": "unit-fixture"},
                nodes=[TraceNode("n1", "A", 2)],
                jobs=[
                    TraceJob("warmup-spot", 0, 20, 2, "Spot", "A"),
                    TraceJob("evaluated-hp", 10, 2, 2, "HP", "A"),
                    TraceJob("evaluated-spot", 12, 2, 2, "Spot", "A"),
                ],
                evaluation_start_seconds=10,
                evaluation_end_seconds=12,
            )
        )
        result = simulate_trace(trace, _policy("fifo"))
        metrics = build_metrics_report(result)

        self.assertEqual(len(result["jobs"]), 3)
        self.assertEqual(metrics["jobs"]["HP"]["job_count"], 1)
        self.assertEqual(metrics["jobs"]["Spot"]["job_count"], 1)
        self.assertEqual(metrics["spot_runs"]["event_count"], 1)
        self.assertGreater(result["cluster"]["warmup_allocated_gpu_seconds"], 0)
        self.assertEqual(result["cluster"]["allocation_rate_mean"], 1.0)

    def test_checked_in_action_space_contains_only_executable_profiles(self):
        action_space_schema = json.loads(
            (PROJECT_ROOT / "schemas" / "action-space.schema.json").read_text(
                encoding="utf-8"
            )
        )
        for filename in (
            "native-v1.json",
            "native-multiwindow-v1.json",
            "native-multiwindow-v2.json",
            "native-multiwindow-v3.json",
        ):
            action_space = json.loads(
                (PROJECT_ROOT / "configs" / "action_spaces" / filename).read_text(
                    encoding="utf-8"
                )
            )
            self.assertLessEqual(
                set(action_space), set(action_space_schema["properties"])
            )
            self.assertEqual(
                action_space["schema_version"], "schednav.native-action-space/v1"
            )
            controlled = set(action_space["controlled_fields"])
            expected_controls = {
                "scheduler",
                "spot_guarantee_seconds",
                "checkpoint_interval_seconds",
            }
            if filename in {"native-multiwindow-v2.json", "native-multiwindow-v3.json"}:
                expected_controls |= {
                    "hp_preemption_delay_seconds",
                    "spot_eviction_budget_rate",
                }
            if filename == "native-multiwindow-v3.json":
                expected_controls.add("preemption_victim_strategy")
            self.assertEqual(controlled, expected_controls)
            fixed = action_space["fixed_execution_controls"]
            policies = []
            for relative in action_space["profiles"]:
                profile_path = (PROJECT_ROOT / relative).resolve()
                self.assertTrue(profile_path.is_relative_to(PROJECT_ROOT))
                policy_value = json.loads(profile_path.read_text(encoding="utf-8"))
                policy = SimulationPolicy.from_dict(policy_value)
                self.assertEqual(
                    policy.preemption_overhead_seconds,
                    fixed["preemption_overhead_seconds"],
                )
                self.assertEqual(policy.placement_strategy, fixed["placement_strategy"])
                policies.append(policy)
            self.assertGreaterEqual(len(policies), 3)
            self.assertLessEqual(len(policies), 5)
            self.assertEqual(len({policy.action_id for policy in policies}), len(policies))
            for excluded in action_space.get("excluded_profiles", []):
                self.assertTrue(excluded["action_id"])
                self.assertTrue(excluded["reason"])


if __name__ == "__main__":
    unittest.main()
