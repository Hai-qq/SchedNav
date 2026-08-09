from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from schednav.cli import main
from schednav.native_trace import TraceJob, TraceNode, write_canonical_trace


class CliTests(unittest.TestCase):
    def test_simulate_writes_first_party_result_and_metrics(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            trace_path = write_canonical_trace(
                root / "trace",
                trace_id="cli-fixture",
                time_origin="2026-01-01 00:00:00",
                source={"dataset": "contract-fixture"},
                nodes=[TraceNode("n1", "A", 2)],
                jobs=[TraceJob("job-1", 0, 10, 1, "HP", "A")],
            )
            policy_path = root / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "schema_version": "schednav.simulation-policy/v1",
                        "action_id": "cli-fifo",
                        "scheduler": "fifo",
                        "spot_guarantee_seconds": 3600,
                        "checkpoint_interval_seconds": 3600,
                        "preemption_overhead_seconds": 80,
                        "placement_strategy": "deterministic_best_fit",
                    }
                ),
                encoding="utf-8",
            )
            result_path = root / "output" / "result.json"
            metrics_path = root / "output" / "metrics.json"
            argv = [
                "schednav",
                "simulate",
                "--trace",
                str(trace_path),
                "--policy",
                str(policy_path),
                "--result",
                str(result_path),
                "--metrics",
                str(metrics_path),
            ]

            with patch("sys.argv", argv), redirect_stdout(StringIO()):
                exit_code = main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                json.loads(result_path.read_text(encoding="utf-8"))["schema_version"],
                "schednav.simulation-result/v1",
            )
            self.assertEqual(
                json.loads(metrics_path.read_text(encoding="utf-8"))["schema_version"],
                "schednav.metrics-report/v2",
            )

    def test_forecast_and_predictive_simulation_commands_emit_control_artifacts(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            trace_path = write_canonical_trace(
                root / "trace",
                trace_id="cli-predictive",
                time_origin="2026-01-01 00:00:00",
                source={"dataset": "predictive-cli-fixture"},
                nodes=[TraceNode("n1", "A", 2)],
                jobs=[TraceJob("job-1", 0, 10, 1, "HP", "A")],
            )
            policy_path = root / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "schema_version": "schednav.simulation-policy/v1",
                        "action_id": "cli-predictive",
                        "scheduler": "priority_preemptive",
                        "spot_guarantee_seconds": 3600,
                        "checkpoint_interval_seconds": 3600,
                        "preemption_overhead_seconds": 80,
                        "placement_strategy": "deterministic_best_fit",
                    }
                ),
                encoding="utf-8",
            )
            controller_path = (
                Path(__file__).resolve().parents[1]
                / "configs"
                / "controllers"
                / "predictive-spot-v1.json"
            )
            forecast_path = root / "output" / "forecast.json"
            with patch(
                "sys.argv",
                [
                    "schednav",
                    "forecast-demand",
                    "--trace",
                    str(trace_path),
                    "--controller",
                    str(controller_path),
                    "--cutoff-seconds",
                    "0",
                    "--output",
                    str(forecast_path),
                ],
            ), redirect_stdout(StringIO()):
                self.assertEqual(main(), 0)
            self.assertEqual(
                json.loads(forecast_path.read_text(encoding="utf-8"))["schema_version"],
                "schednav.predictive-observation-bundle/v1",
            )

            result_path = root / "output" / "predictive-result.json"
            metrics_path = root / "output" / "predictive-metrics.json"
            with patch(
                "sys.argv",
                [
                    "schednav",
                    "simulate-predictive",
                    "--trace",
                    str(trace_path),
                    "--policy",
                    str(policy_path),
                    "--controller",
                    str(controller_path),
                    "--result",
                    str(result_path),
                    "--metrics",
                    str(metrics_path),
                ],
            ), redirect_stdout(StringIO()):
                self.assertEqual(main(), 0)
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            self.assertEqual(
                metrics["predictive_control"]["controller_id"], "predictive-spot-v1"
            )


if __name__ == "__main__":
    unittest.main()
