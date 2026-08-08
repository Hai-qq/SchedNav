import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from schednav.contracts import canonical_sha256
from schednav.policy_compare import compare_policy_metrics


def _metrics(scheduler: str, hp_jct: float) -> dict:
    policy = {
        "scheduler": scheduler,
        "guarantee_hours": [1],
        "guarantee_rate": 0.9,
        "ckpt_interval_seconds": 3600,
        "seed": 42,
    }
    report = {
        "schema_version": "schednav.metrics-report/v1",
        "policy_fingerprint": canonical_sha256(policy),
        "policy": policy,
        "source": {
            "gfs_commit": "a" * 40,
            "gfs_patch_sha256": "b" * 64,
            "trace_commit": "c" * 40,
        },
        "trace_id": "trace",
        "window_seconds": {"start": 1, "end": 2},
        "jobs": {
            "HP": {
                "job_count": 1,
                "completed_count": 1,
                "completion_rate": 1.0,
                "jct_seconds": {"mean": hp_jct, "p50": hp_jct, "p95": hp_jct},
                "queue_seconds": {"mean": 0.0, "p50": 0.0, "p95": 0.0},
                "preemption_count": 0,
                "preempted_job_count": 0,
                "preempted_job_rate": 0.0,
            },
            "Spot": {
                "job_count": 1,
                "completed_count": 1,
                "completion_rate": 1.0,
                "jct_seconds": {"mean": 10.0, "p50": 10.0, "p95": 10.0},
                "queue_seconds": {"mean": 0.0, "p50": 0.0, "p95": 0.0},
                "preemption_count": 0,
                "preempted_job_count": 0,
                "preempted_job_rate": 0.0,
            },
        },
        "cluster": {"allocation_rate_mean": 0.5},
        "preemption_events": {
            "available": True,
            "consistent_with_job_csv": True,
            "added_gpu_seconds_total": 0,
            "eviction_rate_per_run": 0.0,
        },
        "spot_runs": {"available": True, "consistent_with_job_csv": True},
        "spot_guarantee": {
            "available": True,
            "consistent_with_preemption_events": True,
            "success_rate": 1.0,
        },
    }
    report["metrics_fingerprint"] = canonical_sha256(report)
    return report


class PolicyCompareTests(unittest.TestCase):
    def test_compares_matching_populations_without_selecting_a_winner(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            left = root / "left.json"
            right = root / "right.json"
            left.write_text(json.dumps(_metrics("fifo_spot", 20.0)), encoding="utf-8")
            right.write_text(json.dumps(_metrics("spot_scheduler", 15.0)), encoding="utf-8")
            report = compare_policy_metrics(left, right)
            self.assertTrue(report["comparable"])
            self.assertEqual(report["metric_deltas"]["hp_jct_mean_seconds"]["right_minus_left"], -5.0)
            self.assertTrue(report["interpretation_caveats"])
            self.assertNotIn("winner", report)

    def test_compares_distinct_actions_with_the_same_scheduler(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            left = _metrics("spot_scheduler", 20.0)
            right = _metrics("spot_scheduler", 18.0)
            right["policy"]["guarantee_rate"] = 0.95
            right["policy_fingerprint"] = canonical_sha256(right["policy"])
            right.pop("metrics_fingerprint")
            right["metrics_fingerprint"] = canonical_sha256(right)

            left_path = root / "left.json"
            right_path = root / "right.json"
            left_path.write_text(json.dumps(left), encoding="utf-8")
            right_path.write_text(json.dumps(right), encoding="utf-8")

            report = compare_policy_metrics(left_path, right_path)

            self.assertTrue(report["comparable"])
            self.assertTrue(report["criteria"]["policy_actions_distinct"])
            self.assertTrue(report["criteria"]["execution_controls_match"])
            self.assertEqual(report["right"]["action"]["guarantee_rate"], 0.95)


if __name__ == "__main__":
    unittest.main()
