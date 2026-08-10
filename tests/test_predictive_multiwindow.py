from __future__ import annotations

import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from schednav.contracts import canonical_sha256
from schednav.predictive_multiwindow import (
    RUN_RECEIPT_SCHEMA,
    WINDOW_RECORD_SCHEMA,
    build_arm_record,
    build_partition_summary,
    build_selection_lock,
    load_predictive_multiwindow_study,
    verify_selection_lock,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_runner_module():
    path = PROJECT_ROOT / "scripts" / "run_predictive_multiwindow_experiment.py"
    spec = importlib.util.spec_from_file_location("predictive_multiwindow_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _arm(arm_id: str, *, allocation: float, hard_passed: bool = True) -> dict:
    return {
        "arm_id": arm_id,
        "deterministic_repetitions": True,
        "hard_slo_passed": hard_passed,
        "failed_hard_constraints": [] if hard_passed else ["allocation-fifo-nondegradation"],
        "metrics": {
            "allocation_rate_mean": allocation,
            "hp_jct_p95_seconds": 100.0,
            "spot_jct_p95_seconds": 200.0,
            "spot_eviction_rate_per_run": 0.01,
            "spot_guarantee_success_rate": 0.99,
        },
    }


def _record(window_id: str, arms: list[dict]) -> dict:
    value = {
        "schema_version": WINDOW_RECORD_SCHEMA,
        "window_id": window_id,
        "arms": arms,
    }
    value["window_record_fingerprint"] = canonical_sha256(value)
    return value


def _study() -> dict:
    return {
        "design_fingerprint": "1" * 64,
        "arms": [
            {"arm_id": "fifo"},
            {"arm_id": "aggregate-predictive"},
            {"arm_id": "tenant-predictive"},
        ],
        "windows": [
            {"window_id": "c1", "partition": "calibration"},
            {"window_id": "c2", "partition": "calibration"},
            {"window_id": "h1", "partition": "holdout"},
        ],
    }


class PredictiveMultiwindowTests(unittest.TestCase):
    def test_new_contract_schemas_are_valid_json_schema_documents(self) -> None:
        for name in (
            "predictive-multiwindow-study.schema.json",
            "predictive-multiwindow-evidence.schema.json",
        ):
            schema = json.loads(
                (PROJECT_ROOT / "schemas" / name).read_text(encoding="utf-8")
            )
            self.assertEqual(
                schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
            )

    def test_frozen_predictive_study_loads(self) -> None:
        study = load_predictive_multiwindow_study(
            PROJECT_ROOT / "configs" / "studies" / "predictive-multiwindow-v1.json"
        )

        self.assertEqual(len(study["windows"]), 11)
        self.assertEqual(
            [item["partition"] for item in study["windows"]],
            ["calibration"] * 6 + ["holdout"] * 5,
        )
        self.assertIs(
            study["prior_window_selection"]["selected_before_predictive_simulation"],
            True,
        )
        self.assertIs(
            study["execution"]["future_arrivals_visible_to_controllers"], False
        )

    def test_study_rejects_a_missing_policy_path(self) -> None:
        source = json.loads(
            (
                PROJECT_ROOT
                / "configs"
                / "studies"
                / "predictive-multiwindow-v1.json"
            ).read_text(encoding="utf-8")
        )
        source["arms"][0]["policy"] = None
        source["design_fingerprint"] = canonical_sha256(
            {key: value for key, value in source.items() if key != "design_fingerprint"}
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "study.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-empty project-relative path"):
                load_predictive_multiwindow_study(path)

    def test_arm_record_rejects_duplicate_repetition_receipts(self) -> None:
        receipts = []
        for _ in range(2):
            receipt = {
                "schema_version": RUN_RECEIPT_SCHEMA,
                "arm_id": "tenant-predictive",
                "kind": "predictive",
                "repetition": 1,
                "policy_fingerprint": "2" * 64,
                "result_fingerprint": "3" * 64,
                "metrics_fingerprint": "4" * 64,
                "future_arrivals_visible_to_controller": False,
            }
            receipt["run_receipt_fingerprint"] = canonical_sha256(receipt)
            receipts.append(receipt)

        with self.assertRaisesRegex(ValueError, "each declared repetition"):
            build_arm_record(
                {
                    "arm_id": "tenant-predictive",
                    "kind": "predictive",
                    "role": "test",
                },
                receipts,
                {},
                {},
            )

    def test_calibration_selection_preserves_unresolved_ties(self) -> None:
        study = _study()
        records = [
            _record(
                window_id,
                [
                    _arm("fifo", allocation=0.70),
                    _arm("aggregate-predictive", allocation=0.71),
                    _arm("tenant-predictive", allocation=0.71),
                ],
            )
            for window_id in ("c1", "c2")
        ]

        summary = build_partition_summary(
            study, "calibration", records, allocation_tie_band=0.01
        )

        selection = summary["calibration_selection"]
        self.assertEqual(selection["status"], "tie_requires_human_approval")
        self.assertEqual(
            selection["selected_arm_ids"],
            ["aggregate-predictive", "tenant-predictive"],
        )
        self.assertIs(selection["weighted_score_used"], False)

    def test_calibration_rejects_arms_with_any_hard_slo_failure(self) -> None:
        study = _study()
        records = [
            _record(
                "c1",
                [
                    _arm("fifo", allocation=0.70, hard_passed=False),
                    _arm("aggregate-predictive", allocation=0.71, hard_passed=False),
                    _arm("tenant-predictive", allocation=0.72, hard_passed=False),
                ],
            ),
            _record(
                "c2",
                [
                    _arm("fifo", allocation=0.70),
                    _arm("aggregate-predictive", allocation=0.71),
                    _arm("tenant-predictive", allocation=0.72),
                ],
            ),
        ]

        summary = build_partition_summary(
            study, "calibration", records, allocation_tie_band=0.01
        )

        self.assertEqual(
            summary["calibration_selection"]["status"], "no_eligible_arm"
        )
        self.assertEqual(summary["calibration_selection"]["selected_arm_ids"], [])

    def test_selection_lock_is_content_addressed(self) -> None:
        study = _study()
        records = [
            _record(
                window_id,
                [
                    _arm("fifo", allocation=0.70),
                    _arm("aggregate-predictive", allocation=0.71),
                    _arm("tenant-predictive", allocation=0.72),
                ],
            )
            for window_id in ("c1", "c2")
        ]
        calibration = build_partition_summary(
            study, "calibration", records, allocation_tie_band=0.01
        )

        lock = build_selection_lock(study, calibration)
        verify_selection_lock(study, lock)
        lock["holdout_result_count_at_freeze"] = 1

        with self.assertRaisesRegex(ValueError, "Selection lock is invalid"):
            verify_selection_lock(study, lock)

    def test_existing_pre_holdout_lock_allows_runner_resume(self) -> None:
        runner = _load_runner_module()
        study = copy.deepcopy(_study())
        for index, window in enumerate(study["windows"], start=1):
            window["date"] = f"2024-01-{index:02d}"
        records = [
            _record(
                window_id,
                [
                    _arm("fifo", allocation=0.70),
                    _arm("aggregate-predictive", allocation=0.71),
                    _arm("tenant-predictive", allocation=0.72),
                ],
            )
            for window_id in ("c1", "c2")
        ]
        calibration = build_partition_summary(
            study, "calibration", records, allocation_tie_band=0.01
        )
        lock = build_selection_lock(study, calibration)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "calibration-summary.json").write_text(
                json.dumps(calibration), encoding="utf-8"
            )
            (output / "selection-lock.json").write_text(
                json.dumps(lock), encoding="utf-8"
            )
            (output / "windows" / "2024-01-03" / "runs").mkdir(parents=True)

            with redirect_stdout(io.StringIO()):
                resumed = runner.freeze(study, output)
            self.assertEqual(resumed, lock)
