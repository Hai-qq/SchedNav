import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from schednav.window_scan import scan_eviction_candidates


class WindowScanTests(unittest.TestCase):
    def test_ranks_early_spot_and_late_hp_pressure(self):
        with TemporaryDirectory() as temp_dir:
            trace_dir = Path(temp_dir)
            with (trace_dir / "node_info_df.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["gpu_model", "gpu_capacity_num", "node_name"])
                writer.writeheader()
                writer.writerow({"gpu_model": "GPU-X", "gpu_capacity_num": 8, "node_name": "n1"})
            with (trace_dir / "job_info_df.csv").open("w", encoding="utf-8", newline="") as handle:
                fields = ["gpu_model", "gpu_request", "worker_num", "submit_time", "duration", "job_type"]
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                day = 36 * 86_400
                writer.writerows(
                    [
                        {"gpu_model": "GPU-X", "gpu_request": 2, "worker_num": 1, "submit_time": day + 3600, "duration": 14_400, "job_type": "Spot"},
                        {"gpu_model": "GPU-X", "gpu_request": 4, "worker_num": 1, "submit_time": day + 50_000, "duration": 100, "job_type": "HP"},
                    ]
                )
            report = scan_eviction_candidates(trace_dir, "2024-04-06", "2024-04-06", 5)
            candidate = report["candidates"][0]
            self.assertEqual(candidate["gpu_model"], "GPU-X")
            self.assertEqual(candidate["early_spot_gpu_hours"], 8.0)
            self.assertEqual(candidate["late_hp_requested_gpus"], 4.0)
            self.assertEqual(candidate["candidate_score"], 0.041667)


if __name__ == "__main__":
    unittest.main()
