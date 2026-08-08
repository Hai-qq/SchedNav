import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from schednav.contracts import canonical_sha256
from schednav.policy_rank import rank_audited_policies


def _write_report(path: Path, report: dict, fingerprint_key: str) -> None:
    report[fingerprint_key] = canonical_sha256(report)
    path.write_text(json.dumps(report), encoding="utf-8")


class PolicyRankTests(unittest.TestCase):
    def test_preserves_unresolved_tie_after_declared_hierarchy(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            slo = {
                "schema_version": "schednav.slo-spec/v1",
                "name": "ranking-test",
                "constraints": [],
                "ranking": {
                    "allocation_metric": "allocation_rate_mean",
                    "allocation_tie_band": 0.01,
                    "second_metric": "spot_jct_p95_seconds",
                    "third_metric": "spot_eviction_rate_per_run",
                    "unresolved_tie": "human_approval",
                },
            }
            slo_path = root / "slo.json"
            slo_path.write_text(json.dumps(slo), encoding="utf-8")
            slo_fingerprint = canonical_sha256(slo)
            metrics_paths = []
            audit_paths = []
            for index, allocation in enumerate((0.5, 0.805, 0.81)):
                policy = {
                    "scheduler": "priority_preemptive",
                    "spot_guarantee_seconds": index * 1800,
                }
                metrics = {
                    "policy_fingerprint": canonical_sha256(policy),
                    "policy": policy,
                    "cluster": {"allocation_rate_mean": allocation},
                    "jobs": {"Spot": {"jct_seconds": {"p95": 100.0}}},
                    "preemption_events": {"eviction_rate_per_run": 0.02},
                    "spot_guarantee": {"success_rate": 0.99},
                }
                metrics_path = root / f"metrics-{index}.json"
                _write_report(metrics_path, metrics, "metrics_fingerprint")
                audit = {
                    "metrics_fingerprint": metrics["metrics_fingerprint"],
                    "slo_fingerprint": slo_fingerprint,
                    "audit_passed": True,
                    "results": [{"id": "allocation-soft-target", "passed": allocation >= 0.8}],
                }
                audit_path = root / f"audit-{index}.json"
                _write_report(audit_path, audit, "audit_fingerprint")
                metrics_paths.append(metrics_path)
                audit_paths.append(audit_path)

            report = rank_audited_policies(metrics_paths, audit_paths, slo_path)

            self.assertEqual(report["selection_status"], "tie_requires_human_approval")
            self.assertEqual(len(report["selected_policy_fingerprints"]), 2)
            self.assertNotIn("score", report)


if __name__ == "__main__":
    unittest.main()
