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
        for filename in ("native-v1.json", "native-multiwindow-v1.json"):
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
            self.assertEqual(
                controlled,
                {"scheduler", "spot_guarantee_seconds", "checkpoint_interval_seconds"},
            )
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
