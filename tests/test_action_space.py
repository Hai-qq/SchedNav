import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from schednav.action_space import materialize_policy_action, validate_policy_action


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACTION_SPACE_PATH = PROJECT_ROOT / "configs" / "action_spaces" / "v1-baseline.json"
BASE_CONFIG_PATH = PROJECT_ROOT / "configs" / "baselines" / "stress-gpu-series-2-2024-04-12.json"


def _action() -> dict:
    return {
        "schema_version": "schednav.policy-action/v1",
        "action_id": "repository-default-fifo",
        "scheduler": "fifo_spot",
        "guarantee_hours": [1],
        "guarantee_rate": 0.9,
        "ckpt_interval_seconds": 3600,
    }


class ActionSpaceTests(unittest.TestCase):
    def test_materializes_only_bounded_policy_fields(self):
        with TemporaryDirectory() as temp_dir:
            action_path = Path(temp_dir) / "action.json"
            action_path.write_text(json.dumps(_action()), encoding="utf-8")
            run_spec, receipt = materialize_policy_action(BASE_CONFIG_PATH, ACTION_SPACE_PATH, action_path)
            self.assertEqual(run_spec["policy"]["scheduler"], "fifo_spot")
            self.assertEqual(run_spec["window"]["gpu_models"], ["GPU-series-2"])
            self.assertNotIn("node_id", run_spec["policy"])
            self.assertEqual(receipt["controlled_fields"][1], "policy.scheduler")

    def test_rejects_unbounded_field(self):
        action = _action()
        action["node_id"] = "n1"
        action_space = json.loads(ACTION_SPACE_PATH.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "exactly"):
            validate_policy_action(action, action_space)

    def test_rejects_unlisted_cross_product(self):
        action = _action()
        action["guarantee_rate"] = 0.8
        action_space = json.loads(ACTION_SPACE_PATH.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "curated_profile"):
            validate_policy_action(action, action_space)


if __name__ == "__main__":
    unittest.main()
