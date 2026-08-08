from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from schednav.native_trace import TraceJob, TraceNode, load_canonical_trace, write_canonical_trace
from schednav.native_workload import analyze_canonical_workload


class NativeWorkloadTests(unittest.TestCase):
    def test_analyzes_any_canonical_trace_without_dataset_specific_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = write_canonical_trace(
                Path(temporary),
                trace_id="dataset-neutral",
                time_origin="2026-01-01 00:00:00",
                source={"dataset": "another-provider"},
                nodes=[TraceNode("n1", "A", 4)],
                jobs=[
                    TraceJob("hp", 0, 3600, 1.5, "HP", "A"),
                    TraceJob("spot", 1800, 3600, 2, "Spot", "A"),
                ],
            )
            report = analyze_canonical_workload(
                load_canonical_trace(manifest), sample_interval_seconds=1800
            )
            self.assertEqual(report["schema_version"], "schednav.workload-summary/v2")
            self.assertEqual(report["source"]["dataset"], "another-provider")
            self.assertEqual(report["population"]["HP"]["requested_gpus"], 1.5)
            self.assertEqual(report["population"]["Spot"]["requested_gpus"], 2)
            self.assertEqual(report["regime_signals"]["combined_peak_active_pressure"], 0.875)
            self.assertRegex(report["workload_fingerprint"], "^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
