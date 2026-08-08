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


if __name__ == "__main__":
    unittest.main()
