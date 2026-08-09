"""Publish a compact, content-addressed single-trace compatibility receipt."""

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


def build_receipt(trace_path: Path, artifact_root: Path) -> dict[str, Any]:
    trace = _load(trace_path)
    workload = _load(artifact_root / "workload-summary.json")
    portfolio = _load(artifact_root / "policy-portfolio-v2.json")
    ranking = _load(artifact_root / "policy-ranking-v2.json")
    for value, field in (
        (trace, "trace_fingerprint"),
        (workload, "workload_fingerprint"),
        (portfolio, "portfolio_fingerprint"),
        (ranking, "ranking_fingerprint"),
    ):
        _validate_fingerprint(value, field)
    if workload["trace_fingerprint"] != trace["trace_fingerprint"]:
        raise ValueError("Workload and Trace fingerprints do not match")
    if portfolio["source"]["trace_fingerprint"] != trace["trace_fingerprint"]:
        raise ValueError("Portfolio and Trace fingerprints do not match")

    candidates: dict[str, dict[str, Any]] = {}
    for candidate in ranking["candidates"]:
        action_id = str(candidate["action"]["action_id"])
        metrics = _load(artifact_root / f"{action_id}-metrics.json")
        audit = _load(artifact_root / f"{action_id}-slo-audit.json")
        _validate_fingerprint(metrics, "metrics_fingerprint")
        _validate_fingerprint(audit, "audit_fingerprint")
        if candidate["metrics_fingerprint"] != metrics["metrics_fingerprint"]:
            raise ValueError(f"Ranking metrics mismatch for {action_id}")
        candidates[action_id] = {
            "action": candidate["action"],
            "hard_slo_passed": audit["audit_passed"],
            "allocation_rate_mean": metrics["cluster"]["allocation_rate_mean"],
            "hp_jct_p95_seconds": metrics["jobs"]["HP"]["jct_seconds"]["p95"],
            "hp_queue_p95_seconds": metrics["jobs"]["HP"]["queue_seconds"]["p95"],
            "spot_jct_p95_seconds": metrics["jobs"]["Spot"]["jct_seconds"]["p95"],
            "spot_eviction_rate_per_run": metrics["preemption_events"][
                "eviction_rate_per_run"
            ],
            "spot_guarantee_success_rate": metrics["spot_guarantee"][
                "success_rate"
            ],
            "metrics_fingerprint": metrics["metrics_fingerprint"],
            "audit_fingerprint": audit["audit_fingerprint"],
        }

    selected_fingerprints = set(ranking["selected_policy_fingerprints"])
    remaining_action_ids = [
        item["action"]["action_id"]
        for item in ranking["candidates"]
        if item["policy_fingerprint"] in selected_fingerprints
    ]
    receipt: dict[str, Any] = {
        "schema_version": "schednav.trace-evidence/v1",
        "study": {
            "purpose": "cross-trace adapter and deterministic policy-evaluation compatibility",
            "source": trace["source"],
            "trace_id": trace["trace_id"],
            "trace_fingerprint": trace["trace_fingerprint"],
            "capacity_gpus": workload["capacity_gpus"],
            "window_seconds": workload["window_seconds"],
        },
        "workload": {
            "population": {
                service_class: workload["population"][service_class]["job_count"]
                for service_class in ("HP", "Spot")
            },
            "regime_signals": workload["regime_signals"],
            "workload_fingerprint": workload["workload_fingerprint"],
        },
        "experiment": {
            "candidate_count": len(candidates),
            "portfolio_comparable": portfolio["comparable"],
            "all_candidates_passed_hard_slo": all(
                item["hard_slo_passed"] is True for item in candidates.values()
            ),
            "candidates": candidates,
            "portfolio_fingerprint": portfolio["portfolio_fingerprint"],
        },
        "ranking": {
            "selection_status": ranking["selection_status"],
            "remaining_action_ids": remaining_action_ids,
            "ranking_fingerprint": ranking["ranking_fingerprint"],
        },
        "interpretation": {
            "claim_scope": "compatibility_only",
            "result": "All five policies tie because this trace is far below cluster capacity; the run validates ingestion, fractional GPU demand, class mapping, simulation, SLO audit, and tie preservation, not an optimization gain.",
        },
        "limitations": [
            "Source LS and BE QoS are mapped explicitly to HP and Spot; this is a source-semantic mapping, not a claim that the labels are identical to another dataset's service classes.",
            "Durations are observed scheduled-to-deletion occupancy intervals. Failed and still-running source phases are not reinterpreted as successful application completion.",
            "Peak active requested-GPU pressure is below one percent, so this trace does not exercise contention or preemption and cannot demonstrate a scheduling advantage.",
            "Raw source rows and canonical per-job files are not redistributed.",
        ],
    }
    receipt["receipt_fingerprint"] = canonical_sha256(receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--artifact-directory", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output_path = Path(args.output).resolve()
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite evidence: {output_path}")
    receipt = build_receipt(
        Path(args.trace).resolve(), Path(args.artifact_directory).resolve()
    )
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
                "selection_status": receipt["ranking"]["selection_status"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
