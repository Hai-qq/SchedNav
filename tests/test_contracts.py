import json
from pathlib import Path
import unittest

from schednav.contracts import RunSpec


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "configs" / "baselines" / "golden-a800-2024-04-07.json"
STRESS_CONFIG_PATH = PROJECT_ROOT / "configs" / "baselines" / "stress-gpu-series-2-2024-04-12.json"
FIFO_STRESS_CONFIG_PATH = PROJECT_ROOT / "configs" / "baselines" / "stress-fifo-gpu-series-2-2024-04-12.json"


class RunSpecTests(unittest.TestCase):
    def test_golden_config_is_valid_and_stable(self):
        first = RunSpec.load(CONFIG_PATH)
        second = RunSpec.load(CONFIG_PATH)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.policy.minimum_warmup_hours, 864)
        self.assertEqual(first.window.gpu_models, ("A800-SXM4-80GB",))
        self.assertEqual(first.gfs_patch, "patches/gfs/reproduction-gate.patch")

    def test_rejects_insufficient_warmup(self):
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        value["window"]["evaluation_start"] = "2024-03-02 00:00:00"
        with self.assertRaisesRegex(ValueError, "Warm-up"):
            RunSpec.from_dict(value)

    def test_rejects_later_trace_origin_without_snapshot(self):
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        value["window"]["submit_time_origin"] = "2024-04-01 00:00:00"
        with self.assertRaisesRegex(ValueError, "state snapshot"):
            RunSpec.from_dict(value)

    def test_eviction_stress_config_is_valid(self):
        spec = RunSpec.load(STRESS_CONFIG_PATH)
        self.assertEqual(spec.window.gpu_models, ("GPU-series-2",))
        self.assertEqual(spec.window.evaluation_start, "2024-04-12 00:00:00")
        self.assertEqual(spec.window.evaluation_end, "2024-04-12 23:59:59")

    def test_fifo_stress_config_uses_the_same_window(self):
        gfs = RunSpec.load(STRESS_CONFIG_PATH)
        fifo = RunSpec.load(FIFO_STRESS_CONFIG_PATH)
        self.assertEqual(gfs.window, fifo.window)
        self.assertEqual(gfs.policy.scheduler, "spot_scheduler")
        self.assertEqual(fifo.policy.scheduler, "fifo_spot")


if __name__ == "__main__":
    unittest.main()
