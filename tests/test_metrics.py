import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from schednav.contracts import RunSpec
from schednav.metrics import extract_metrics


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "configs" / "baselines" / "golden-a800-2024-04-07.json"


class MetricsTests(unittest.TestCase):
    def test_extracts_evaluation_population_and_allocation(self):
        spec = RunSpec.load(CONFIG_PATH)
        start = spec.window.seconds_from_origin(spec.window.evaluation_start)
        with TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            evidence_dir = run_dir / "gfs-output" / "fixture" / "cluster"
            evidence_dir.mkdir(parents=True)
            manifest_path = run_dir / "run_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "status": "succeeded",
                        "run_id": "fixture",
                        "run_spec_fingerprint": spec.fingerprint,
                        "trace_id": "fixture-trace",
                        "gfs_commit": spec.gfs_commit,
                        "gfs_patch_sha256": "a" * 64,
                        "trace_commit": spec.trace_commit,
                    }
                ),
                encoding="utf-8",
            )
            with (evidence_dir / "spot_scheduler_log.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["job_index", "submit_time", "type", "status", "start_time", "jct", "queue", "preempt_times", "ckpt_times"],
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {"job_index": 1, "submit_time": start, "type": "HP", "status": "end", "start_time": start, "jct": 10, "queue": 2, "preempt_times": 0, "ckpt_times": 0},
                        {"job_index": 2, "submit_time": start + 1, "type": "Spot", "status": "end", "start_time": start + 5, "jct": 20, "queue": 4, "preempt_times": 1, "ckpt_times": 2},
                        {"job_index": 3, "submit_time": start - 1, "type": "HP", "status": "end", "start_time": start, "jct": 999, "queue": 999, "preempt_times": 0, "ckpt_times": 0},
                    ]
                )
            with (evidence_dir / "spot_scheduler_seq.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["time", "gpu_utilization"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"time": start, "gpu_utilization": 0.25},
                        {"time": start + 60, "gpu_utilization": 0.75},
                    ]
                )
            with (evidence_dir / "spot_scheduler_preemption_events.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                fields = [
                    "preempted_submit_time",
                    "preempted_type",
                    "preempted_job_index",
                    "counted_as_spot_failure",
                    "event_in_evaluation_window",
                    "rollback_seconds",
                    "overhead_seconds",
                    "added_gpu_seconds",
                ]
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(
                    {
                        "preempted_submit_time": start + 1,
                        "preempted_type": "Spot",
                        "preempted_job_index": 2,
                        "counted_as_spot_failure": "True",
                        "event_in_evaluation_window": "False",
                        "rollback_seconds": 30,
                        "overhead_seconds": 40,
                        "added_gpu_seconds": 140,
                    }
                )
            with (evidence_dir / "spot_scheduler_spot_run_events.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                fields = [
                    "job_submit_time", "job_type", "job_index", "run_ordinal_for_job",
                    "event_in_evaluation_window",
                ]
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(
                    [
                        {"job_submit_time": start + 1, "job_type": "Spot", "job_index": 2, "run_ordinal_for_job": 1, "event_in_evaluation_window": "True"},
                        {"job_submit_time": start + 1, "job_type": "Spot", "job_index": 2, "run_ordinal_for_job": 2, "event_in_evaluation_window": "False"},
                    ]
                )
            with (evidence_dir / "spot_scheduler_spot_guarantee_events.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                fields = ["job_submit_time", "job_type", "outcome"]
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(
                    [
                        {"job_submit_time": start + 1, "job_type": "Spot", "outcome": "succeeded"},
                        {"job_submit_time": start + 1, "job_type": "Spot", "outcome": "failed"},
                    ]
                )
            report = extract_metrics(spec, manifest_path)
            self.assertEqual(report["jobs"]["HP"]["job_count"], 1)
            self.assertEqual(report["jobs"]["Spot"]["preempted_job_rate"], 1.0)
            self.assertEqual(report["cluster"]["allocation_rate_mean"], 0.5)
            self.assertEqual(report["cluster"]["sample_interval_seconds"], 60)
            self.assertEqual(report["source"]["gfs_commit"], spec.gfs_commit)
            self.assertTrue(report["preemption_events"]["consistent_with_job_csv"])
            self.assertEqual(report["preemption_events"]["events_during_drain"], 1)
            self.assertEqual(report["preemption_events"]["added_gpu_seconds_total"], 140)
            self.assertEqual(report["preemption_events"]["spot_run_count"], 2)
            self.assertEqual(report["preemption_events"]["eviction_rate_per_run"], 0.5)
            self.assertTrue(report["spot_runs"]["consistent_with_job_csv"])
            self.assertEqual(report["spot_runs"]["events_during_drain"], 1)
            self.assertEqual(report["spot_guarantee"]["success_rate"], 0.5)
            self.assertTrue(report["spot_guarantee"]["consistent_with_preemption_events"])


if __name__ == "__main__":
    unittest.main()
