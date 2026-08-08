import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from schednav.cli import main
from schednav.contracts import canonical_sha256
from schednav.eviction_gate import evaluate_eviction_gate


class EvictionGateTests(unittest.TestCase):
    def _write_metrics(self, root: Path, preemptions: int) -> Path:
        metrics = {
            "schema_version": "schednav.metrics-report/v1",
            "jobs": {
                "Spot": {
                    "job_count": 2,
                    "completed_count": 2,
                    "preemption_count": preemptions,
                    "preempted_job_count": int(preemptions > 0),
                }
            },
            "preemption_events": {
                "available": True,
                "consistent_with_job_csv": True,
                "counted_spot_failure_count": preemptions,
                "preempted_job_count": int(preemptions > 0),
                "added_gpu_seconds_total": 100 if preemptions else 0,
            },
            "evidence": {
                "job_csv_sha256": "a" * 64,
                "sequence_csv_sha256": "b" * 64,
                "preemption_event_csv_sha256": "c" * 64,
            },
        }
        metrics["metrics_fingerprint"] = canonical_sha256(metrics)
        path = root / "metrics.json"
        path.write_text(json.dumps(metrics), encoding="utf-8")
        return path

    def test_passes_with_attested_preemption(self):
        with TemporaryDirectory() as temp_dir:
            report = evaluate_eviction_gate(self._write_metrics(Path(temp_dir), 2))
            self.assertTrue(report["gate_passed"])

    def test_fails_without_preemption(self):
        with TemporaryDirectory() as temp_dir:
            report = evaluate_eviction_gate(self._write_metrics(Path(temp_dir), 0))
            self.assertFalse(report["gate_passed"])
            self.assertFalse(report["criteria"]["spot_preemption_observed"])

    def test_cli_returns_nonzero_for_failed_gate(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            metrics_path = self._write_metrics(root, 0)
            output_path = root / "gate.json"
            argv = [
                "schednav",
                "eviction-gate",
                "--metrics",
                str(metrics_path),
                "--output",
                str(output_path),
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(StringIO()):
                self.assertEqual(main(), 1)
            self.assertFalse(json.loads(output_path.read_text(encoding="utf-8"))["gate_passed"])


if __name__ == "__main__":
    unittest.main()
