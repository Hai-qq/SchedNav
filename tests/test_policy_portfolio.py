import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from schednav.contracts import canonical_sha256
from schednav.policy_portfolio import compare_policy_portfolio


def _metrics(rate: float) -> dict:
    policy = {
        "scheduler": "spot_scheduler",
        "guarantee_hours": [1],
        "guarantee_rate": rate,
        "ckpt_interval_seconds": 3600,
        "seed": 42,
    }
    report = {
        "schema_version": "schednav.metrics-report/v1",
        "run_spec_fingerprint": canonical_sha256({"rate": rate}),
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
            job_type: {
                "job_count": 1,
                "completed_count": 1,
                "completion_rate": 1.0,
                "jct_seconds": {"mean": 10.0 + rate, "p50": 10.0, "p95": 10.0},
                "queue_seconds": {"mean": 0.0, "p50": 0.0, "p95": 0.0},
                "preemption_count": 0,
                "preempted_job_count": 0,
                "preempted_job_rate": 0.0,
            }
            for job_type in ("HP", "Spot")
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


class PolicyPortfolioTests(unittest.TestCase):
    def test_compares_three_unique_actions_without_ranking(self):
        with TemporaryDirectory() as temp_dir:
            paths = []
            for index, rate in enumerate((0.8, 0.9, 0.95)):
                path = Path(temp_dir) / f"metrics-{index}.json"
                path.write_text(json.dumps(_metrics(rate)), encoding="utf-8")
                paths.append(path)

            report = compare_policy_portfolio(paths)

            self.assertTrue(report["comparable"])
            self.assertEqual(len(report["candidates"]), 3)
            self.assertEqual(len(report["pairwise"]), 3)
            self.assertNotIn("winner", report)
            self.assertNotIn("ranking", report)

    def test_requires_three_to_five_candidates(self):
        with self.assertRaisesRegex(ValueError, "between 3 and 5"):
            compare_policy_portfolio([])


if __name__ == "__main__":
    unittest.main()
