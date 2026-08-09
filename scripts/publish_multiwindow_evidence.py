"""Publish a compact, content-addressed multi-window evidence receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT_DEFAULT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from schednav.contracts import canonical_sha256


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_fingerprint(value: dict[str, Any], field: str) -> None:
    supplied = value.get(field)
    payload = {key: item for key, item in value.items() if key != field}
    if not isinstance(supplied, str) or canonical_sha256(payload) != supplied:
        raise ValueError(f"Invalid {field}: {supplied}")


def _failed_constraints(window_root: Path, action_id: str) -> list[str]:
    audit = _load(window_root / f"{action_id}-slo-audit.json")
    return [
        str(item["id"])
        for item in audit["results"]
        if item["severity"] == "hard" and item["passed"] is False
    ]


def build_receipt(experiment_root: Path) -> dict[str, Any]:
    selection = _load(experiment_root / "window-selection.json")
    summary = _load(experiment_root / "multiwindow-summary.json")
    manifest = _load(experiment_root / "experiment-manifest.json")
    _validate_fingerprint(selection, "selection_fingerprint")
    _validate_fingerprint(summary, "multiwindow_fingerprint")
    _validate_fingerprint(manifest, "experiment_fingerprint")
    if summary["selection_fingerprint"] != selection["selection_fingerprint"]:
        raise ValueError("Summary and selection fingerprints do not match")
    if manifest["multiwindow_fingerprint"] != summary["multiwindow_fingerprint"]:
        raise ValueError("Manifest and summary fingerprints do not match")

    windows: list[dict[str, Any]] = []
    for record in sorted(summary["windows"], key=lambda item: item["date"]):
        policies = {item["action_id"]: item for item in record["policies"]}
        fifo_allocation = float(policies[summary["baseline_action_id"]]["allocation_rate_mean"])
        eligible = [item for item in policies.values() if item["hard_slo_passed"] is True]
        best_allocation = (
            max(float(item["allocation_rate_mean"]) for item in eligible)
            if eligible
            else None
        )
        window_root = experiment_root / "windows" / str(record["date"])
        windows.append(
            {
                "date": record["date"],
                "stratum": record["stratum"],
                "population": record["population"],
                "regime_signals": record["regime_signals"],
                "fifo_allocation_rate": fifo_allocation,
                "best_hard_pass_allocation_rate": best_allocation,
                "best_hard_pass_allocation_uplift_vs_fifo": (
                    best_allocation - fifo_allocation
                    if best_allocation is not None
                    else None
                ),
                "ranking": record["ranking"],
                "policies": {
                    action_id: {
                        "hard_slo_passed": policy["hard_slo_passed"],
                        "failed_hard_constraints": _failed_constraints(
                            window_root, action_id
                        ),
                        "allocation_rate_mean": policy["allocation_rate_mean"],
                        "hp_jct_p95_seconds": policy["hp_jct_p95_seconds"],
                        "hp_queue_p95_seconds": policy["hp_queue_p95_seconds"],
                        "spot_jct_p95_seconds": policy["spot_jct_p95_seconds"],
                        "spot_eviction_rate_per_run": policy[
                            "spot_eviction_rate_per_run"
                        ],
                        "spot_guarantee_success_rate": policy[
                            "spot_guarantee_success_rate"
                        ],
                        "metrics_fingerprint": policy["metrics_fingerprint"],
                    }
                    for action_id, policy in sorted(policies.items())
                },
                "window_record_fingerprint": record["window_record_fingerprint"],
            }
        )

    receipt: dict[str, Any] = {
        "schema_version": "schednav.multiwindow-evidence/v1",
        "study": {
            "dataset": manifest["source"]["dataset"],
            "dataset_commit": manifest["source"]["commit"],
            "source_hashes": {
                "node_info_sha256": manifest["source"]["node_info_sha256"],
                "job_info_sha256": manifest["source"]["job_info_sha256"],
            },
            "gpu_models": selection["source"]["filter"]["gpu_models"],
            "capacity_gpus": selection["capacity_gpus"],
            "eligibility": selection["eligibility"],
            "eligible_window_count": selection["eligible_window_count"],
            "selection_method": selection["selection_method"],
            "selected_window_count": selection["selected_window_count"],
            "warmup_seconds": manifest["warmup_seconds"],
            "action_space": manifest["action_space"],
            "repetitions_per_policy_per_window": manifest[
                "repetitions_per_policy_per_window"
            ],
        },
        "aggregate": {
            "window_count": summary["window_count"],
            "all_hard_slo_pass_window_count": summary[
                "all_hard_slo_pass_window_count"
            ],
            "selection_status_counts": summary["selection_status_counts"],
            "frontier_action_window_frequency": summary[
                "frontier_action_window_frequency"
            ],
            "best_hard_pass_allocation_uplift_vs_fifo": summary[
                "best_hard_pass_allocation_uplift_vs_fifo"
            ],
            "policies": summary["policies"],
            "universal_winner_declared": False,
        },
        "windows": windows,
        "limitations": [
            "This is historical, offline counterfactual evaluation, not online scheduling.",
            "Each selected day uses a fixed 30-day warm-up, so jobs submitted earlier than the warm-up boundary are not reconstructed.",
            "native-preemptive-1800 was excluded by the declared 600-second evaluation-resource gate; this is not an SLO failure result.",
            "A no_eligible_policy result is preserved rather than forcing a recommendation.",
        ],
        "source_evidence": {
            "selection_fingerprint": selection["selection_fingerprint"],
            "multiwindow_fingerprint": summary["multiwindow_fingerprint"],
            "experiment_fingerprint": manifest["experiment_fingerprint"],
        },
    }
    receipt["receipt_fingerprint"] = canonical_sha256(receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-directory", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    experiment_root = Path(args.experiment_directory).resolve()
    output_path = Path(args.output).resolve()
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite evidence: {output_path}")
    receipt = build_receipt(experiment_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "receipt_fingerprint": receipt["receipt_fingerprint"],
                "window_count": receipt["aggregate"]["window_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
