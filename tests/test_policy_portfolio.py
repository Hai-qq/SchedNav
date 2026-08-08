import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from schednav.contracts import canonical_sha256
from schednav.policy_portfolio import compare_policy_portfolio


def _metrics(guarantee_seconds: int) -> dict:
    policy = {
        "schema_version": "schednav.simulation-policy/v1",
        "action_id": f"policy-{guarantee_seconds}",
        "scheduler": "priority_preemptive",
        "spot_guarantee_seconds": guarantee_seconds,
        "checkpoint_interval_seconds": 3600,
        "preemption_overhead_seconds": 80,
        "placement_strategy": "deterministic_best_fit",
    }
    report = {
        "schema_version": "schednav.metrics-report/v2",
        "run_spec_fingerprint": canonical_sha256({"guarantee_seconds": guarantee_seconds}),
        "policy_fingerprint": canonical_sha256(policy),
        "policy": policy,
        "source": {
            "dataset": "contract-fixture",
            "engine": {"name": "schednav-sim", "version": "1"},
            "trace_fingerprint": "c" * 64,
        },
        "trace_id": "trace",
        "window_seconds": {"evaluation_start": 1, "evaluation_end": 2},
        "jobs": {
            job_type: {
                "job_count": 1,
                "completed_count": 1,
                "completion_rate": 1.0,
                "jct_seconds": {
                    "mean": 10.0 + guarantee_seconds / 3600,
                    "p50": 10.0,
                    "p95": 10.0,
                },
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
            for index, guarantee_seconds in enumerate((0, 1800, 3600)):
                path = Path(temp_dir) / f"metrics-{index}.json"
                path.write_text(json.dumps(_metrics(guarantee_seconds)), encoding="utf-8")
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
