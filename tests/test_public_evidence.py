from __future__ import annotations

import json
from pathlib import Path
import unittest

from schednav.contracts import canonical_sha256


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PublicEvidenceTests(unittest.TestCase):
    def test_representative_policy_receipt_is_content_addressed_and_approval_pending(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "native-v1"
            / "alibaba-gpu-series-2-2024-04-12-policy-evaluation.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(receipt["slo"]["hard_constraint_count"], 8)
        self.assertTrue(receipt["slo"]["all_candidates_passed_all_hard_constraints"])
        self.assertEqual(len(receipt["experiment"]["policies"]), 4)
        self.assertEqual(
            receipt["ranking"]["selection_status"],
            "tie_requires_human_approval",
        )
        self.assertEqual(len(receipt["ranking"]["remaining_action_ids"]), 3)

    def test_multiwindow_receipt_is_content_addressed_and_preserves_outcomes(self):
        path = (
            PROJECT_ROOT
            / "evidence"
            / "native-v1"
            / "alibaba-gpu-series-2-multiwindow-30d-v1.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_fingerprint")

        self.assertEqual(canonical_sha256(receipt), supplied)
        self.assertEqual(receipt["schema_version"], "schednav.multiwindow-evidence/v1")
        self.assertEqual(receipt["study"]["eligible_window_count"], 112)
        self.assertEqual(receipt["study"]["selected_window_count"], 12)
        self.assertEqual(receipt["study"]["repetitions_per_policy_per_window"], 2)
        self.assertEqual(receipt["aggregate"]["window_count"], 12)
        self.assertEqual(
            receipt["aggregate"]["selection_status_counts"],
            {
                "no_eligible_policy": 1,
                "selected": 5,
                "tie_requires_human_approval": 6,
            },
        )
        uplift = receipt["aggregate"]["best_hard_pass_allocation_uplift_vs_fifo"]
        self.assertEqual(uplift["positive_window_count"], 5)
        self.assertEqual(uplift["equal_window_count"], 6)
        self.assertEqual(uplift["negative_window_count"], 0)
        self.assertFalse(receipt["aggregate"]["universal_winner_declared"])
        self.assertTrue(
            all(
                policy["deterministic_window_count"] == 12
                for policy in receipt["aggregate"]["policies"].values()
            )
        )
        self.assertEqual(
            receipt["study"]["action_space"]["excluded_profiles"][0]["action_id"],
            "native-preemptive-1800",
        )


if __name__ == "__main__":
    unittest.main()
