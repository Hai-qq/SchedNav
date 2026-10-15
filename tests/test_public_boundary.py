import unittest
from scripts.check_public_boundary import violations_for


class PublicBoundaryTests(unittest.TestCase):
    def test_task_ids_are_not_api_keys(self):
        self.assertEqual(violations_for("evidence/receipt.json", b'task-20260811-214000'), [])

    def test_credentials_are_detected_without_echoing_values(self):
        for content in (b'sk-' + b'a' * 30, b'Bearer ' + b'a' * 32, b'ghp_' + b'b' * 40):
            self.assertIn("possible credential", violations_for("config.json", content))

    def test_private_data_and_credentials_paths_are_rejected(self):
        for path in ("datasets/raw.json", "jobs.csv", ".env.local", "artifacts/result.json"):
            self.assertTrue(violations_for(path, b"{}"))
