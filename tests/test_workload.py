import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from schednav.workload import analyze_workload


class WorkloadTests(unittest.TestCase):
    def test_reports_hp_carry_in_and_window_spot_demand(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (root / "node_info_df.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["gpu_model", "gpu_capacity_num"])
                writer.writeheader()
                writer.writerow({"gpu_model": "GPU-X", "gpu_capacity_num": 8})
            with (root / "job_info_df.csv").open("w", encoding="utf-8", newline="") as handle:
                fields = ["gpu_model", "job_type", "submit_time", "duration", "gpu_request", "worker_num"]
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(
                    [
                        {"gpu_model": "GPU-X", "job_type": "HP", "submit_time": 0, "duration": 7200, "gpu_request": 2, "worker_num": 1},
                        {"gpu_model": "GPU-X", "job_type": "Spot", "submit_time": 3600, "duration": 3600, "gpu_request": 1, "worker_num": 2},
                    ]
                )
            report = analyze_workload(
                root,
                "GPU-X",
                "2024-03-01 01:00:00",
                "2024-03-01 02:00:00",
                3600,
            )
            self.assertEqual(report["population"]["HP"]["job_count"], 0)
            self.assertEqual(report["population"]["Spot"]["requested_gpus"], 2.0)
            self.assertEqual(report["regime_signals"]["combined_peak_active_pressure"], 0.5)


if __name__ == "__main__":
    unittest.main()
