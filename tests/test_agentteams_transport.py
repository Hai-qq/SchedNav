"""Regression checks for fail-closed external transport handling."""
import unittest
from scripts.agentteams_transport import (
    artifact_read_satisfied, transient_mcp_error, truncated_artifact_receipt,
)


class TransportBoundaryTests(unittest.TestCase):
    def test_near_cap_and_non_read_responses_fail(self):
        for size in (65_535, 65_537):
            with self.assertRaises(ValueError):
                truncated_artifact_receipt("read_artifact", {"artifact_ref": "x"}, "x" * size)
        with self.assertRaises(ValueError):
            truncated_artifact_receipt("advance_rolling_policy", {"artifact_ref": "x"}, "x" * 65_536)

    def test_complete_json_at_cap_is_not_an_omission(self):
        with self.assertRaises(ValueError):
            truncated_artifact_receipt("read_artifact", {"artifact_ref": "x"}, '"' + 'x' * 65_534 + '"')

    def test_omission_never_satisfies_metrics(self):
        receipt = truncated_artifact_receipt("read_artifact", {"artifact_ref": "x"}, "x" * 65_536)
        self.assertFalse(artifact_read_satisfied("metrics", receipt))
        receipt.pop("requires_trusted_collector")
        self.assertFalse(artifact_read_satisfied("rolling_control_report", receipt))

    def test_auth_and_semantic_errors_fail_closed(self):
        for error in ("SSE error: Non-200 status code (401)", "invalid artifact_ref", {"message": "404"}):
            self.assertFalse(transient_mcp_error("read_artifact", {"error": error}))
