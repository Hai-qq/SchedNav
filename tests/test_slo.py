import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from schednav.contracts import canonical_sha256
from schednav.slo import audit_slo


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SloAuditTests(unittest.TestCase):
    def test_demo_slo_v1_is_locked(self):
        slo = json.loads(
            (PROJECT_ROOT / "configs" / "slos" / "schednav-demo-slo-v1.json").read_text(
                encoding="utf-8"
            )
        )
        constraints = {item["id"]: item for item in slo["constraints"]}
        self.assertEqual(sum(item["severity"] == "hard" for item in constraints.values()), 8)
        self.assertEqual(sum(item["severity"] == "soft" for item in constraints.values()), 1)
        self.assertEqual(constraints["hp-p95-jct-fifo-regression"]["threshold"]["multiplier"], 1.01)
        self.assertEqual(constraints["hp-p95-queue-seconds"]["threshold"], 3600)
        self.assertEqual(constraints["spot-eviction-rate-per-run"]["threshold"], 0.1)
        self.assertEqual(constraints["spot-guarantee-success-rate"]["threshold"], 0.9)
        self.assertEqual(constraints["allocation-soft-target"]["threshold"], 0.8)
        self.assertEqual(slo["ranking"]["allocation_tie_band"], 0.01)
        self.assertEqual(slo["ranking"]["unresolved_tie"], "human_approval")

    def test_hard_and_soft_constraints_are_explicit(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            metrics = {
                "schema_version": "schednav.metrics-report/v1",
                "metrics_fingerprint": "placeholder",
                "trace_id": "fixture-trace",
                "policy_fingerprint": "fixture-policy",
                "policy": {"scheduler": "spot_scheduler"},
                "source": {"gfs_commit": "a", "gfs_patch_sha256": "b", "trace_commit": "c"},
                "window_seconds": {"start": 1, "end": 2},
                "jobs": {
                    "HP": {"job_count": 1, "jct_seconds": {"p95": 90.0}},
                    "Spot": {"job_count": 1},
                },
                "cluster": {"allocation_rate_mean": 0.5},
                "preemption_events": {"available": True, "consistent_with_job_csv": True},
                "spot_runs": {"available": True, "consistent_with_job_csv": True},
                "spot_guarantee": {"available": True, "consistent_with_preemption_events": True},
            }
            metrics["metrics_fingerprint"] = canonical_sha256(
                {key: value for key, value in metrics.items() if key != "metrics_fingerprint"}
            )
            slo = {
                "schema_version": "schednav.slo-spec/v1",
                "name": "synthetic-test-only",
                "constraints": [
                    {"id": "hp", "metric": "hp_jct_p95_seconds", "operator": "<=", "threshold": 100, "severity": "hard"},
                    {"id": "alloc", "metric": "allocation_rate_mean", "operator": ">=", "threshold": 0.8, "severity": "soft"},
                ],
            }
            metrics_path = root / "metrics.json"
            slo_path = root / "slo.json"
            metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
            slo_path.write_text(json.dumps(slo), encoding="utf-8")
            report = audit_slo(metrics_path, slo_path)
            self.assertTrue(report["audit_passed"])
            self.assertTrue(report["metrics_schema_supported"])
            self.assertEqual(report["scheduler"], "spot_scheduler")
            self.assertEqual(report["soft_violation_count"], 1)

            metrics["spot_runs"]["available"] = False
            metrics["metrics_fingerprint"] = canonical_sha256(
                {key: value for key, value in metrics.items() if key != "metrics_fingerprint"}
            )
            metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
            missing_run_ledger_report = audit_slo(metrics_path, slo_path)
            self.assertFalse(missing_run_ledger_report["audit_passed"])
            self.assertFalse(
                missing_run_ledger_report["evidence_checks"]["spot_run_ledger_consistent"]
            )

    def test_resolves_relative_threshold_from_compatible_fifo_baseline(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common = {
                "schema_version": "schednav.metrics-report/v1",
                "source": {"gfs_commit": "a", "gfs_patch_sha256": "b", "trace_commit": "c"},
                "trace_id": "fixture-trace",
                "window_seconds": {"start": 1, "end": 2},
                "jobs": {
                    "HP": {"job_count": 1, "jct_seconds": {"p95": 101.0}},
                    "Spot": {"job_count": 1},
                },
                "cluster": {},
                "preemption_events": {"available": True, "consistent_with_job_csv": True},
                "spot_runs": {"available": True, "consistent_with_job_csv": True},
                "spot_guarantee": {"available": True, "consistent_with_preemption_events": True},
            }
            metrics = {**common, "policy_fingerprint": "candidate", "policy": {"scheduler": "spot_scheduler"}}
            baseline = json.loads(json.dumps(common))
            baseline.update({"policy_fingerprint": "fifo", "policy": {"scheduler": "fifo_spot"}})
            for report in (metrics, baseline):
                report["metrics_fingerprint"] = canonical_sha256(report)
            slo = {
                "schema_version": "schednav.slo-spec/v1",
                "name": "relative-test-only",
                "constraints": [{
                    "id": "hp-relative",
                    "metric": "hp_jct_p95_seconds",
                    "operator": "<=",
                    "threshold": {
                        "kind": "baseline_relative",
                        "baseline_metric": "hp_jct_p95_seconds",
                        "multiplier": 1.01,
                    },
                    "severity": "hard",
                }],
            }
            metrics_path = root / "metrics.json"
            baseline_path = root / "baseline.json"
            slo_path = root / "slo.json"
            metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
            baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
            slo_path.write_text(json.dumps(slo), encoding="utf-8")

            report = audit_slo(metrics_path, slo_path, baseline_path)

            self.assertTrue(report["audit_passed"])
            self.assertEqual(report["results"][0]["threshold"], 102.01)
            self.assertTrue(report["baseline"]["compatible_fifo"])


if __name__ == "__main__":
    unittest.main()
