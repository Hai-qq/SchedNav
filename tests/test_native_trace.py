from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest

from schednav.native_trace import (
    import_alibaba_gpu_v2023_trace,
    TraceJob,
    TraceNode,
    import_alibaba_trace,
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

    def test_imports_alibaba_v2023_qos_without_synthetic_class_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nodes = root / "nodes.csv"
            pods = root / "pods.csv"
            nodes.write_text(
                "sn,cpu_milli,memory_mib,gpu,model\n"
                "n0,64000,262144,2,P100\n"
                "n1,64000,262144,0,\n",
                encoding="utf-8",
            )
            pods.write_text(
                "name,cpu_milli,memory_mib,num_gpu,gpu_milli,gpu_spec,qos,pod_phase,creation_time,deletion_time,scheduled_time\n"
                "hp,1000,1024,1,1000,,LS,Succeeded,100,210,110\n"
                "spot,1000,1024,1,250,,BE,Running,120,250,130\n"
                "pending,1000,1024,1,1000,,BE,Pending,140,200,\n"
                "other,1000,1024,1,1000,,Burstable,Succeeded,150,300,160\n",
                encoding="utf-8",
            )
            trace = load_canonical_trace(
                import_alibaba_gpu_v2023_trace(nodes, pods, root / "trace")
            )

            self.assertEqual(trace.capacity_gpus, 2)
            jobs = {job.job_id: job for job in trace.jobs}
            self.assertEqual(set(jobs), {"hp", "spot"})
            self.assertEqual(jobs["hp"].service_class, "HP")
            self.assertEqual(jobs["spot"].service_class, "Spot")
            self.assertEqual(jobs["spot"].gpu_count, 0.25)
            self.assertEqual(jobs["hp"].duration_seconds, 100)
            self.assertEqual(
                trace.source["service_class_mapping"],
                {
                    "LS": "HP",
                    "BE": "Spot",
                    "basis": "Source-published Latency Sensitive and Best Effort QoS labels.",
                },
            )
            self.assertIn("occupancy interval", trace.source["duration_semantics"])
            self.assertEqual(trace.source["skipped_rows"]["excluded_phase"], 1)
            self.assertEqual(trace.source["skipped_rows"]["unsupported_qos"], 1)

    def test_alibaba_v2023_rejects_an_empty_phase_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nodes = root / "nodes.csv"
            pods = root / "pods.csv"
            nodes.write_text("sn,gpu,model\nn0,1,A100\n", encoding="utf-8")
            pods.write_text(
                "name,num_gpu,gpu_milli,qos,pod_phase,creation_time,deletion_time,scheduled_time\n"
                "pod,1,1000,LS,Running,0,10,0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "included_phases"):
                import_alibaba_gpu_v2023_trace(
                    nodes,
                    pods,
                    root / "trace",
                    included_phases=set(),
                )

    def test_round_trips_an_explicit_evaluation_window_with_warmup_arrivals(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = write_canonical_trace(
                root,
                trace_id="windowed",
                time_origin="2026-01-01 00:00:00",
                source={"dataset": "unit-fixture"},
                nodes=[TraceNode("n1", "A", 2)],
                jobs=[
                    TraceJob("warmup", 0, 20, 1, "HP", "A"),
                    TraceJob("evaluated", 10, 2, 1, "Spot", "A"),
                ],
                evaluation_start_seconds=10,
                evaluation_end_seconds=20,
            )
            trace = load_canonical_trace(manifest)
            self.assertEqual(trace.evaluation_start_seconds, 10)
            self.assertEqual(trace.evaluation_end_seconds, 20)
            value = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                value["evaluation_window_seconds"], {"start": 10.0, "end": 20.0}
            )

            prefix = load_canonical_trace(
                slice_canonical_trace(
                    trace,
                    root / "prefix",
                    trace_id="warmup-only-prefix",
                    max_submit_time_seconds=10,
                )
            )
            self.assertIsNone(prefix.evaluation_start_seconds)
            self.assertIsNone(prefix.evaluation_end_seconds)

    def test_imports_alibaba_warmup_and_records_the_explicit_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node_path = root / "node_info_df.csv"
            with node_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["node_name", "gpu_model", "gpu_capacity_num"])
                writer.writerow(["n1", "GPU-series-2", 8])
            job_path = root / "job_info_df.csv"
            with job_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    [
                        "job_name",
                        "gpu_model",
                        "gpu_request",
                        "worker_num",
                        "submit_time",
                        "duration",
                        "job_type",
                    ]
                )
                writer.writerow(["warmup", "GPU-series-2", 1, 1, 0, 20, "Spot"])
                writer.writerow(["evaluated", "GPU-series-2", 2, 1, 10, 2, "HP"])
                writer.writerow(["post-window", "GPU-series-2", 1, 1, 21, 2, "HP"])

            trace = load_canonical_trace(
                import_alibaba_trace(
                    node_path,
                    job_path,
                    root / "canonical",
                    gpu_models={"GPU-series-2"},
                    evaluation_start_seconds=10,
                    evaluation_end_seconds=20,
                )
            )

            self.assertEqual([job.job_id for job in trace.jobs], ["warmup", "evaluated"])
            self.assertEqual(trace.evaluation_start_seconds, 10)
            self.assertEqual(trace.evaluation_end_seconds, 20)
            self.assertEqual(trace.source["filter"]["max_submit_time_seconds"], 20)

            bounded = load_canonical_trace(
                import_alibaba_trace(
                    node_path,
                    job_path,
                    root / "bounded-warmup",
                    gpu_models={"GPU-series-2"},
                    evaluation_start_seconds=10,
                    evaluation_end_seconds=20,
                    warmup_start_seconds=5,
                )
            )
            self.assertEqual([job.job_id for job in bounded.jobs], ["evaluated"])
            self.assertEqual(bounded.source["filter"]["warmup_start_seconds"], 5)

    def test_rejects_invalid_alibaba_warmup_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node_path = root / "node_info_df.csv"
            job_path = root / "job_info_df.csv"
            node_path.write_text(
                "node_name,gpu_model,gpu_capacity_num\nn1,GPU-series-2,8\n",
                encoding="utf-8",
            )
            job_path.write_text(
                "job_name,gpu_model,gpu_request,worker_num,submit_time,duration,job_type\n"
                "j1,GPU-series-2,1,1,10,2,HP\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "warm-up start"):
                import_alibaba_trace(
                    node_path,
                    job_path,
                    root / "invalid",
                    evaluation_start_seconds=10,
                    evaluation_end_seconds=20,
                    warmup_start_seconds=11,
                )


if __name__ == "__main__":
    unittest.main()
