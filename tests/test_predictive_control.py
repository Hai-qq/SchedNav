from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from schednav.native_simulator import (
    SimulationPolicy,
    build_metrics_report,
    simulate_trace,
)
from schednav.native_trace import TraceJob, TraceNode, load_canonical_trace, write_canonical_trace
from schednav.predictive_control import (
    PredictiveControllerConfig,
    PredictiveSpotController,
    build_observation_bundle,
)


def _config(**overrides) -> PredictiveControllerConfig:
    value = {
        "schema_version": "schednav.predictive-controller/v1",
        "controller_id": "test-predictive",
        "model": "seasonal-gaussian-v1",
        "observation_interval_seconds": 300,
        "aggregation_interval_seconds": 3600,
        "lookback_hours": 168,
        "forecast_horizon_hours": 4,
        "retrain_interval_seconds": 86400,
        "guarantee_probability": 0.9,
        "guarantee_horizons_hours": [1, 2, 4],
        "minimum_history_hours": 1,
        "minimum_sigma_gpus": 0.0,
        "initial_eta": 1.0,
        "minimum_eta": 0.25,
        "maximum_eta": 1.25,
        "feedback_window_seconds": 14400,
        "starvation_increase_after_seconds": 3600,
        "high_eviction_ratio": 1.5,
        "low_eviction_ratio": 0.5,
        "eta_increase_step": 0.1,
    }
    value.update(overrides)
    return PredictiveControllerConfig.from_dict(value)


class PredictiveControlTests(unittest.TestCase):
    def test_checked_in_controller_locks_the_v1_cadence_and_probability(self):
        project_root = Path(__file__).resolve().parents[1]
        config = PredictiveControllerConfig.load(
            project_root / "configs" / "controllers" / "predictive-spot-v1.json"
        )
        self.assertEqual(config.observation_interval_seconds, 300)
        self.assertEqual(config.lookback_hours, 672)
        self.assertEqual(config.forecast_horizon_hours, 4)
        self.assertEqual(config.retrain_interval_seconds, 86400)
        self.assertEqual(config.guarantee_probability, 0.9)
        for name in (
            "predictive-controller.schema.json",
            "predictive-observation-bundle.schema.json",
            "predictive-control-report.schema.json",
            "predictive-run.schema.json",
        ):
            schema = json.loads((project_root / "schemas" / name).read_text(encoding="utf-8"))
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_quota_reserves_the_probability_bound_and_feedback_reduces_eta(self):
        controller = PredictiveSpotController(_config(), 10, 0)
        with self.assertRaisesRegex(ValueError, "guarantee exceeds"):
            controller.quota_for_guarantee_seconds(5 * 3600)
        first = controller.update(
            0,
            hp_outstanding_requested_gpus=6,
            spot_backlog_gpus=4,
            running_spot_gpus=0,
        )
        self.assertEqual(
            first["quota_plan"]["spot_quota_gpus_by_guarantee_hour"],
            {"1": 4, "2": 4, "4": 4},
        )
        controller.observe_spot_run_end(100, evicted=True)
        second = controller.update(
            300,
            hp_outstanding_requested_gpus=6,
            spot_backlog_gpus=4,
            running_spot_gpus=0,
        )
        self.assertEqual(second["feedback"]["adjustment_reason"], "high-eviction-decrease")
        self.assertEqual(second["feedback"]["eta"], 0.25)
        self.assertEqual(second["quota_plan"]["spot_quota_gpus_by_guarantee_hour"]["1"], 1)

    def test_future_jobs_do_not_change_a_cutoff_observation_or_forecast(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = [
                TraceJob("hp-past", 0, 600, 2, "HP", "A"),
                TraceJob("spot-past", 300, 900, 1, "Spot", "A"),
            ]
            first_path = write_canonical_trace(
                root / "first",
                trace_id="same-prefix",
                time_origin="2026-01-01 00:00:00",
                source={"dataset": "future-fence-fixture"},
                nodes=[TraceNode("n1", "A", 4)],
                jobs=[*common, TraceJob("future-a", 7200, 600, 4, "HP", "A")],
            )
            second_path = write_canonical_trace(
                root / "second",
                trace_id="same-prefix",
                time_origin="2026-01-01 00:00:00",
                source={"dataset": "future-fence-fixture"},
                nodes=[TraceNode("n1", "A", 4)],
                jobs=[*common, TraceJob("future-b", 7200, 7200, 1, "Spot", "A")],
            )
            first = build_observation_bundle(
                load_canonical_trace(first_path), _config(), 3600
            )
            second = build_observation_bundle(
                load_canonical_trace(second_path), _config(), 3600
            )
            self.assertEqual(first, second)
            self.assertTrue(
                first["information_boundary"][
                    "jobs_with_submit_time_after_cutoff_excluded"
                ]
            )
            self.assertFalse(
                first["information_boundary"]["full_trace_fingerprint_exposed"]
            )

    def test_simulator_enforces_predictive_admission_and_emits_scored_evidence(self):
        with TemporaryDirectory() as temporary:
            trace_path = write_canonical_trace(
                Path(temporary),
                trace_id="predictive-sim",
                time_origin="2026-01-01 00:00:00",
                source={"dataset": "predictive-fixture"},
                nodes=[TraceNode("n1", "A", 4)],
                jobs=[
                    TraceJob("spot", 0, 1200, 2, "Spot", "A"),
                    TraceJob("hp", 300, 300, 4, "HP", "A"),
                ],
                evaluation_start_seconds=0,
                evaluation_end_seconds=1200,
            )
            trace = load_canonical_trace(trace_path)
            policy = SimulationPolicy.from_dict(
                {
                    "schema_version": "schednav.simulation-policy/v1",
                    "action_id": "predictive-policy",
                    "scheduler": "priority_preemptive",
                    "spot_guarantee_seconds": 0,
                    "checkpoint_interval_seconds": 300,
                    "preemption_overhead_seconds": 0,
                    "placement_strategy": "deterministic_best_fit",
                }
            )
            controller = PredictiveSpotController(_config(), trace.capacity_gpus, 0)
            result = simulate_trace(trace, policy, controller)
            metrics = build_metrics_report(result)
            self.assertIn("predictive_control", result)
            self.assertTrue(
                result["predictive_control"]["information_boundary"][
                    "forecast_scoring_occurs_after_target_observation"
                ]
            )
            self.assertGreater(result["predictive_control"]["update_count"], 1)
            self.assertEqual(
                len(result["predictive_control"]["decisions"]),
                result["predictive_control"]["update_count"],
            )
            self.assertEqual(result["predictive_control"]["first_cutoff_time_seconds"], 0)
            self.assertLessEqual(
                result["predictive_control"]["last_cutoff_time_seconds"], 1200
            )
            self.assertGreaterEqual(
                result["predictive_control"]["total_runtime_update_count"],
                result["predictive_control"]["update_count"],
            )
            self.assertEqual(
                metrics["predictive_control"]["controller_id"], "test-predictive"
            )
            self.assertEqual(metrics["jobs"]["HP"]["completion_rate"], 1.0)
            self.assertEqual(metrics["jobs"]["Spot"]["completion_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
