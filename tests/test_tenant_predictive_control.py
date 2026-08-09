from pathlib import Path
from tempfile import TemporaryDirectory
import importlib.util
import json
import unittest

from schednav.controller_factory import create_predictive_controller
from schednav.native_simulator import SimulationPolicy, build_metrics_report, simulate_trace
from schednav.native_trace import (
    TraceJob,
    TraceNode,
    load_canonical_trace,
    write_canonical_trace,
)
from schednav.tenant_predictive_control import (
    TenantPredictiveControllerConfig,
    TenantPredictiveSpotController,
    aggregate_independent_gaussians,
    build_tenant_observation_bundle,
    feedback_eta,
    quota_from_quantiles,
)


def _config(**overrides) -> TenantPredictiveControllerConfig:
    value = {
        "schema_version": "schednav.tenant-predictive-controller/v1",
        "controller_id": "tenant-test-v1",
        "model": "tenant-linear-gaussian-v1",
        "demand_sample_interval_seconds": 60,
        "quota_update_interval_seconds": 300,
        "aggregation_interval_seconds": 3600,
        "lookback_hours": 2,
        "forecast_horizon_hours": 1,
        "retrain_interval_seconds": 86400,
        "validation_hours": 1,
        "training_stride_seconds": 300,
        "guarantee_probability": 0.9,
        "guarantee_horizons_hours": [1],
        "business_calendar": "weekday",
        "moving_average_window": 3,
        "embedding_dimension": 2,
        "attention_hidden_dimension": 2,
        "train_epochs": 1,
        "batch_size": 2,
        "learning_rate": 0.0001,
        "early_stopping_patience": 1,
        "random_seed": 7,
        "nonzero_targets_only": True,
        "initial_eta": 1.0,
        "minimum_eta": 0.25,
        "maximum_eta": 1.25,
        "feedback_window_seconds": 14400,
        "queue_wait_threshold_seconds": 3600,
        "high_eviction_multiple": 1.5,
        "low_eviction_multiple": 0.5,
        "runtime_inventory_cap": True,
    }
    value.update(overrides)
    return TenantPredictiveControllerConfig.from_dict(value)


class TenantPredictiveControlTests(unittest.TestCase):
    def test_checked_in_profile_locks_the_source_cadence_and_model_shape(self):
        root = Path(__file__).resolve().parents[1]
        config = TenantPredictiveControllerConfig.load(
            root / "configs" / "controllers" / "tenant-predictive-spot-v1.json"
        )
        self.assertEqual(config.demand_sample_interval_seconds, 60)
        self.assertEqual(config.quota_update_interval_seconds, 300)
        self.assertEqual(config.aggregation_interval_seconds, 3600)
        self.assertEqual(config.lookback_hours, 672)
        self.assertEqual(config.forecast_horizon_hours, 4)
        self.assertEqual(config.retrain_interval_seconds, 86400)
        self.assertEqual(config.validation_hours, 168)
        self.assertEqual(config.training_stride_seconds, 300)
        self.assertEqual(config.guarantee_horizons_hours, (1, 2, 4))
        self.assertEqual(config.business_calendar, "china")
        self.assertEqual(config.moving_average_window, 25)
        self.assertEqual(config.embedding_dimension, 8)
        self.assertEqual(config.attention_hidden_dimension, 16)
        self.assertEqual(config.train_epochs, 10)
        self.assertEqual(config.batch_size, 32)
        self.assertEqual(config.learning_rate, 0.00004)
        self.assertEqual(config.early_stopping_patience, 3)
        self.assertTrue(config.nonzero_targets_only)
        self.assertEqual(config.guarantee_probability, 0.9)
        self.assertEqual(config.initial_eta, 1.0)
        self.assertEqual(config.minimum_eta, 0.25)
        self.assertEqual(config.maximum_eta, 1.25)
        self.assertEqual(config.feedback_window_seconds, 14400)
        self.assertEqual(config.queue_wait_threshold_seconds, 3600)
        self.assertEqual(config.high_eviction_multiple, 1.5)
        self.assertEqual(config.low_eviction_multiple, 0.5)
        self.assertTrue(config.runtime_inventory_cap)
        self.assertEqual(config.minimum_training_hours, 844)
        schema = json.loads(
            (root / "schemas" / "tenant-predictive-controller.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["properties"]["model"]["const"], "tenant-linear-gaussian-v1"
        )

    def test_probability_aggregation_feedback_and_runtime_quota_match_contract(self):
        config = _config()
        mean, sigma = aggregate_independent_gaussians([(10.0, 3.0), (5.0, 4.0)])
        self.assertEqual(mean, 15.0)
        self.assertEqual(sigma, 5.0)

        eta, reason = feedback_eta(1.0, 0.20, 0.0, config)
        self.assertAlmostEqual(eta, 0.5)
        self.assertEqual(reason, "high_eviction")
        eta, reason = feedback_eta(1.0, 0.02, 3601.0, config)
        self.assertEqual(eta, 1.25)
        self.assertEqual(reason, "low_eviction_high_queue")

        quota, predicted_free = quota_from_quantiles(
            capacity_gpus=100,
            guarantee_quantiles_gpus=[30.8, 40.2],
            horizon_hours=2,
            eta=1.0,
            idle_gpus=20,
            running_spot_gpus=5,
            runtime_inventory_cap=True,
        )
        self.assertEqual(predicted_free, 60)
        self.assertEqual(quota, 25)

    def test_tenant_profile_rejects_an_aggregate_v1_trace(self):
        config = _config()
        with TemporaryDirectory() as temporary:
            trace_path = write_canonical_trace(
                Path(temporary) / "trace",
                trace_id="aggregate-v1",
                time_origin="2024-03-01 00:00:00",
                source={"dataset": "synthetic-contract-fixture"},
                nodes=[TraceNode("n1", "A", 8)],
                jobs=[TraceJob("hp", 0, 60, 1, "HP", "A")],
            )
            trace = load_canonical_trace(trace_path)
            with self.assertRaisesRegex(ValueError, "schednav.trace/v2"):
                create_predictive_controller(
                    config,
                    trace,
                    0,
                    evidence_start_seconds=0,
                    evidence_end_seconds=60,
                )

    def test_feedback_ledger_is_pool_scoped_and_node_weighted(self):
        controller = TenantPredictiveSpotController(
            _config(), {"A": 8}, 0, "2024-03-01 00:00:00"
        )
        controller.observe_spot_run_end(
            3600,
            evicted=False,
            resource_pool="A",
            event_weight=2,
            event_kind="guarantee_duration_completed",
        )
        controller.observe_spot_run_end(
            4000,
            evicted=True,
            resource_pool="A",
            event_weight=2,
            event_kind="preempted",
        )
        feedback = controller.finalize()["feedback_events"]["A"]

        self.assertEqual(feedback["event_count"], 2)
        self.assertEqual(feedback["success_weight"], 2.0)
        self.assertEqual(feedback["failure_weight"], 2.0)
        with self.assertRaisesRegex(ValueError, "known resource pool"):
            controller.observe_spot_run_end(
                5000,
                evicted=False,
                resource_pool="missing",
            )

    def test_offline_cutoff_must_align_with_a_quota_decision(self):
        with TemporaryDirectory() as temporary:
            trace_path = write_canonical_trace(
                Path(temporary) / "trace",
                trace_id="misaligned-cutoff",
                time_origin="2024-03-01 00:00:00",
                source={"dataset": "synthetic-contract-fixture"},
                nodes=[TraceNode("n1", "A", 8)],
                jobs=[TraceJob("hp", 0, 20000, 2, "HP", "A", "tenant-a")],
            )
            with self.assertRaisesRegex(ValueError, "align with quota updates"):
                build_tenant_observation_bundle(
                    load_canonical_trace(trace_path), _config(), 14460
                )

    @unittest.skipUnless(
        importlib.util.find_spec("torch") is not None,
        "optional forecast dependencies are not installed",
    )
    def test_trainable_bundle_is_deterministic_and_future_blind(self):
        config = _config()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            nodes = [TraceNode("n1", "A", 8)]
            prefix = [
                TraceJob("hp-a", 0, 20000, 2, "HP", "A", "tenant-a"),
                TraceJob("hp-b", 0, 20000, 1, "HP", "A", "tenant-b"),
                TraceJob("spot-now", 14400, 1200, 1, "Spot", "A", "tenant-s"),
            ]
            first_path = write_canonical_trace(
                root / "first",
                trace_id="same-prefix",
                time_origin="2024-03-01 00:00:00",
                source={"dataset": "synthetic-contract-fixture"},
                nodes=nodes,
                jobs=[
                    *prefix,
                    TraceJob("future-a", 18000, 600, 4, "HP", "A", "tenant-c"),
                ],
            )
            second_path = write_canonical_trace(
                root / "second",
                trace_id="same-prefix",
                time_origin="2024-03-01 00:00:00",
                source={"dataset": "synthetic-contract-fixture"},
                nodes=nodes,
                jobs=[
                    *prefix,
                    TraceJob("future-b", 18000, 600, 1, "Spot", "A", "tenant-z"),
                ],
            )
            first = build_tenant_observation_bundle(
                load_canonical_trace(first_path), config, 14400
            )
            second = build_tenant_observation_bundle(
                load_canonical_trace(second_path), config, 14400
            )

            self.assertEqual(
                first["observed_prefix_fingerprint"],
                second["observed_prefix_fingerprint"],
            )
            self.assertEqual(
                first["observation_bundle_fingerprint"],
                second["observation_bundle_fingerprint"],
            )
            self.assertFalse(
                first["information_boundary"][
                    "actual_future_demand_used_for_prediction"
                ]
            )
            model = first["demand_forecast"]["model"]
            self.assertEqual(model["model"], "tenant-linear-gaussian-v1")
            self.assertRegex(model["model_fingerprint"], r"^[0-9a-f]{64}$")
            self.assertEqual(model["series_count"], 2)
            self.assertEqual(
                {point["resource_pool"] for point in first["demand_forecast"]["points"]},
                {"A"},
            )

    @unittest.skipUnless(
        importlib.util.find_spec("torch") is not None,
        "optional forecast dependencies are not installed",
    )
    def test_trainable_controller_executes_inside_the_simulator(self):
        config = _config()
        with TemporaryDirectory() as temporary:
            trace_path = write_canonical_trace(
                Path(temporary) / "trace",
                trace_id="tenant-simulator",
                time_origin="2024-03-01 00:00:00",
                source={"dataset": "synthetic-contract-fixture"},
                nodes=[TraceNode("n1", "A", 8)],
                jobs=[
                    TraceJob("hp-a", 0, 20000, 2, "HP", "A", "tenant-a"),
                    TraceJob("hp-b", 0, 20000, 1, "HP", "A", "tenant-b"),
                    TraceJob("spot", 14400, 600, 1, "Spot", "A", "tenant-s"),
                ],
                evaluation_start_seconds=14400,
                evaluation_end_seconds=15000,
            )
            trace = load_canonical_trace(trace_path)
            policy = SimulationPolicy.from_dict(
                {
                    "schema_version": "schednav.simulation-policy/v1",
                    "action_id": "tenant-test-policy",
                    "scheduler": "priority_preemptive",
                    "spot_guarantee_seconds": 3600,
                    "checkpoint_interval_seconds": 3600,
                    "preemption_overhead_seconds": 40,
                    "placement_strategy": "deterministic_best_fit",
                }
            )
            controller = create_predictive_controller(
                config,
                trace,
                0,
                evidence_start_seconds=14400,
                evidence_end_seconds=15000,
            )
            result = simulate_trace(trace, policy, controller)
            metrics = build_metrics_report(result)

            self.assertEqual(
                result["predictive_control"]["model"]["model"],
                "tenant-linear-gaussian-v1",
            )
            self.assertGreater(result["predictive_control"]["demand_sample_count"], 240)
            self.assertEqual(result["jobs"][-1]["tenant_id"], "tenant-s")
            self.assertEqual(metrics["jobs"]["Spot"]["completion_rate"], 1.0)
            self.assertEqual(metrics["predictive_control"]["update_count"], 3)

    @unittest.skipUnless(
        importlib.util.find_spec("torch") is not None,
        "optional forecast dependencies are not installed",
    )
    def test_retraining_cadence_warm_starts_the_model(self):
        config = _config(retrain_interval_seconds=300)
        controller = TenantPredictiveSpotController(
            config, {"A": 8}, 0, "2024-03-01 00:00:00"
        )
        controller.bind_evidence_window(14400, 15000)
        series = json.dumps(["A", "tenant-a"], separators=(",", ":"))
        metadata = {
            series: {"pool": "A", "cluster": "cluster", "tenant": "tenant-a"}
        }

        for now in range(0, 15001, 60):
            controller.update(
                now,
                hp_outstanding_requested_gpus=2,
                spot_backlog_gpus=0,
                running_spot_gpus=0,
                hp_running_requested_gpus_by_series={series: 2},
                demand_series_metadata=metadata,
                spot_backlog_gpus_by_pool={},
                running_spot_gpus_by_pool={},
                idle_gpus_by_pool={"A": 6},
                maximum_spot_queue_wait_seconds_by_pool={},
            )

        report = controller.finalize()
        self.assertEqual(controller.estimator.training_generation, 3)
        self.assertEqual(report["update_count"], 3)
        self.assertEqual(report["model"]["training"]["training_generation"], 3)

    @unittest.skipUnless(
        importlib.util.find_spec("torch") is not None,
        "optional forecast dependencies are not installed",
    )
    def test_feedback_records_each_guarantee_period_and_completion(self):
        config = _config()
        with TemporaryDirectory() as temporary:
            trace_path = write_canonical_trace(
                Path(temporary) / "trace",
                trace_id="tenant-periodic-feedback",
                time_origin="2024-03-01 00:00:00",
                source={"dataset": "synthetic-contract-fixture"},
                nodes=[TraceNode("n1", "A", 8)],
                jobs=[
                    TraceJob("hp-a", 0, 20000, 2, "HP", "A", "tenant-a"),
                    TraceJob("hp-b", 0, 20000, 1, "HP", "A", "tenant-b"),
                    TraceJob("spot", 14400, 7500, 1, "Spot", "A", "tenant-s"),
                ],
                evaluation_start_seconds=14400,
                evaluation_end_seconds=15000,
            )
            trace = load_canonical_trace(trace_path)
            policy = SimulationPolicy.from_dict(
                {
                    "schema_version": "schednav.simulation-policy/v1",
                    "action_id": "tenant-feedback-policy",
                    "scheduler": "priority_preemptive",
                    "spot_guarantee_seconds": 3600,
                    "checkpoint_interval_seconds": 3600,
                    "preemption_overhead_seconds": 40,
                    "placement_strategy": "deterministic_best_fit",
                }
            )
            controller = create_predictive_controller(
                config,
                trace,
                0,
                evidence_start_seconds=14400,
                evidence_end_seconds=15000,
            )
            result = simulate_trace(trace, policy, controller)
            feedback = result["predictive_control"]["feedback_events"]["A"]

            self.assertEqual(feedback["event_count"], 3)
            self.assertEqual(feedback["success_weight"], 3.0)
            self.assertEqual(feedback["failure_weight"], 0.0)
            self.assertEqual(
                feedback["weight_by_event_kind"],
                {"guarantee_duration_completed": 2.0, "job_completed": 1.0},
            )


if __name__ == "__main__":
    unittest.main()
