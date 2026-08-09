from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from schednav.multiwindow import (
    aggregate_multiwindow_records,
    build_window_selection_report,
)
from schednav.native_trace import (
    TraceJob,
    TraceNode,
    load_canonical_trace,
    write_canonical_trace,
)


class MultiwindowTests(unittest.TestCase):
    def test_selects_one_pre_simulation_medoid_per_stratification_cell(self):
        with tempfile.TemporaryDirectory() as temporary:
            day = 86400
            jobs = []
            for index in range(5):
                jobs.extend(
                    [
                        TraceJob(
                            f"hp-{index}",
                            index * day,
                            3600,
                            1 + index,
                            "HP",
                            "A",
                        ),
                        TraceJob(
                            f"spot-{index}",
                            index * day,
                            3600,
                            1 + index * 2,
                            "Spot",
                            "A",
                        ),
                    ]
                )
            jobs.append(TraceJob("sentinel", 5 * day - 1, 1, 1, "HP", "A"))
            trace = load_canonical_trace(
                write_canonical_trace(
                    Path(temporary),
                    trace_id="multiwindow-fixture",
                    time_origin="2026-01-01 00:00:00",
                    source={"dataset": "unit-fixture"},
                    nodes=[TraceNode("n1", "A", 32)],
                    jobs=jobs,
                )
            )
            selection = build_window_selection_report(
                trace,
                min_hp_jobs=1,
                min_spot_jobs=1,
                pressure_strata=2,
                spot_share_strata=2,
            )

            self.assertEqual(selection["eligible_window_count"], 5)
            self.assertEqual(selection["selected_window_count"], 4)
            self.assertEqual(
                {
                    (
                        item["stratum"]["pressure"],
                        item["stratum"]["spot_share"],
                    )
                    for item in selection["selected_windows"]
                },
                {(1, 1), (1, 2), (2, 1), (2, 2)},
            )
            self.assertRegex(selection["selection_fingerprint"], "^[0-9a-f]{64}$")

    def test_selects_every_eligible_window_before_simulation(self):
        with tempfile.TemporaryDirectory() as temporary:
            day = 86400
            jobs = []
            for index in range(3):
                jobs.extend(
                    [
                        TraceJob(f"hp-{index}", index * day, 60, 1, "HP", "A"),
                        TraceJob(
                            f"spot-{index}", index * day, 60, 1, "Spot", "A"
                        ),
                    ]
                )
            jobs.append(TraceJob("sentinel", 3 * day - 1, 1, 1, "HP", "A"))
            trace = load_canonical_trace(
                write_canonical_trace(
                    Path(temporary),
                    trace_id="all-eligible-fixture",
                    time_origin="2026-01-01 00:00:00",
                    source={"dataset": "unit-fixture"},
                    nodes=[TraceNode("n1", "A", 4)],
                    jobs=jobs,
                )
            )

            selection = build_window_selection_report(
                trace,
                min_hp_jobs=1,
                min_spot_jobs=1,
                selection_mode="all-eligible",
            )

            self.assertEqual(selection["eligible_window_count"], 3)
            self.assertEqual(selection["selected_window_count"], 3)
            self.assertEqual(
                selection["selection_method"]["name"],
                "all-eligible-origin-aligned",
            )
            self.assertEqual(
                [item["stratum"]["ordinal"] for item in selection["selected_windows"]],
                [1, 2, 3],
            )

    def test_aggregates_robustness_without_declaring_a_universal_winner(self):
        def record(window_id: str, fifo: float, preemptive: float) -> dict:
            return {
                "window_id": window_id,
                "policies": [
                    {
                        "action_id": "native-fifo",
                        "deterministic_repetitions": True,
                        "hard_slo_passed": True,
                        "allocation_soft_target_met": fifo >= 0.8,
                        "allocation_rate_mean": fifo,
                        "hp_jct_p95_seconds": 100,
                        "spot_eviction_rate_per_run": 0,
                    },
                    {
                        "action_id": "native-preemptive",
                        "deterministic_repetitions": True,
                        "hard_slo_passed": True,
                        "allocation_soft_target_met": preemptive >= 0.8,
                        "allocation_rate_mean": preemptive,
                        "hp_jct_p95_seconds": 100,
                        "spot_eviction_rate_per_run": 0.05,
                    },
                ],
                "ranking": {
                    "selection_status": "tie_requires_human_approval",
                    "selected_action_ids": ["native-fifo", "native-preemptive"],
                },
            }

        summary = aggregate_multiwindow_records(
            [record("w1", 0.7, 0.8), record("w2", 0.9, 0.85)],
            selection_fingerprint="a" * 64,
        )

        self.assertEqual(summary["window_count"], 2)
        self.assertEqual(
            summary["policies"]["native-preemptive"]["allocation_delta_vs_fifo"][
                "positive_window_count"
            ],
            1,
        )
        self.assertEqual(
            summary["policies"]["native-preemptive"]["allocation_delta_vs_fifo"][
                "negative_window_count"
            ],
            1,
        )
        self.assertNotIn("winner", summary)
        self.assertRegex(summary["multiwindow_fingerprint"], "^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
