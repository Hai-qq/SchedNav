from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest

from schednav.native_trace import (
    TraceJob,
    TraceNode,
    import_philly_trace,
    load_canonical_trace,
    slice_canonical_trace,
    write_canonical_trace,
)


class NativeTraceTests(unittest.TestCase):
    def test_round_trips_a_content_addressed_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = write_canonical_trace(
                root,
                trace_id="mixed-small",
                time_origin="2026-01-01 00:00:00",
                source={"dataset": "unit-fixture"},
                nodes=[TraceNode("n2", "A", 2), TraceNode("n1", "A", 2)],
                jobs=[
                    TraceJob("spot", 0, 10, 2, "Spot", "A"),
                    TraceJob("hp", 2, 2, 2, "HP", "A"),
                ],
            )
            trace = load_canonical_trace(manifest)
            self.assertEqual(trace.trace_id, "mixed-small")
            self.assertEqual(trace.capacity_gpus, 4)
            self.assertEqual([job.job_id for job in trace.jobs], ["spot", "hp"])
            self.assertRegex(trace.fingerprint, "^[0-9a-f]{64}$")

            jobs_path = root / "jobs.csv"
            jobs_path.write_text(jobs_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hashes"):
                load_canonical_trace(manifest)

    def test_slices_an_origin_preserving_prefix_with_parent_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = write_canonical_trace(
                root / "full",
                trace_id="full",
                time_origin="2026-01-01 00:00:00",
                source={"dataset": "unit-fixture"},
                nodes=[TraceNode("n1", "A", 4)],
                jobs=[
                    TraceJob("first", 0, 10, 1, "HP", "A"),
                    TraceJob("second", 20, 10, 1, "Spot", "A"),
                ],
            )
            full = load_canonical_trace(manifest)
            sliced = load_canonical_trace(
                slice_canonical_trace(
                    full,
                    root / "slice",
                    trace_id="prefix",
                    max_submit_time_seconds=10,
                )
            )
            self.assertEqual([job.job_id for job in sliced.jobs], ["first"])
            self.assertEqual(sliced.time_origin, full.time_origin)
            self.assertEqual(
                sliced.source["canonical_slice"]["parent_trace_fingerprint"],
                full.fingerprint,
            )

    def test_imports_philly_without_inventing_a_priority_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            machine_path = root / "cluster_machine_list"
            with machine_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["machineId", "number of GPUs", "single GPU mem"])
                writer.writerow(["m1", "8", "24GB"])
            job_path = root / "cluster_job_log"
            job_path.write_text(
                json.dumps(
                    [
                        {
                            "jobid": "application-1",
                            "submitted_time": "2017-10-07 01:11:39",
                            "attempts": [
                                {
                                    "start_time": "2017-10-07 01:12:09",
                                    "end_time": "2017-10-07 01:13:23",
                                    "detail": [{"ip": "m1", "gpus": ["gpu0", "gpu1"]}],
                                }
                            ],
                        },
                        {
                            "jobid": "incomplete",
                            "submitted_time": "2017-10-07 01:12:00",
                            "attempts": [],
                        },
                    ]
                ),
                encoding="utf-8",
            )
            manifest = import_philly_trace(
                job_path,
                machine_path,
                root / "canonical",
                service_class="HP",
            )
            trace = load_canonical_trace(manifest)
            self.assertEqual(trace.jobs[0].duration_seconds, 74)
            self.assertEqual(trace.jobs[0].gpu_count, 2)
            self.assertEqual(trace.jobs[0].service_class, "HP")
            self.assertEqual(
                trace.source["service_class_mapping"]["mode"], "caller_supplied_constant"
            )
            self.assertEqual(trace.source["skipped_job_count"], 1)
            self.assertEqual(trace.source["license"], "CC-BY-4.0")

            with self.assertRaisesRegex(ValueError, "service_class"):
                import_philly_trace(
                    job_path,
                    machine_path,
                    root / "invalid",
                    service_class="made-up",
                )


if __name__ == "__main__":
    unittest.main()
