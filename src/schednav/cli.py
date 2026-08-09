"""Command-line entry point for SchedNav trace analysis and simulation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .native_simulator import run_native_simulation
from .native_trace import (
    import_alibaba_gpu_v2023_trace,
    import_alibaba_trace,
    import_philly_trace,
    load_canonical_trace,
    slice_canonical_trace,
)
from .native_workload import analyze_trace_file
from .policy_compare import compare_policy_metrics
from .policy_portfolio import compare_policy_portfolio
from .policy_rank import rank_audited_policies
from .slo import audit_slo


def _path(value: str) -> Path:
    return Path(value).resolve()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="SchedNav GPU scheduling control plane")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_trace_parser = subparsers.add_parser(
        "validate-trace", help="validate a canonical SchedNav trace"
    )
    validate_trace_parser.add_argument("--trace", required=True)

    philly_parser = subparsers.add_parser(
        "import-philly", help="convert a Microsoft Philly trace into the canonical trace contract"
    )
    philly_parser.add_argument("--job-log", required=True)
    philly_parser.add_argument("--machine-list", required=True)
    philly_parser.add_argument("--output-dir", required=True)
    philly_parser.add_argument("--service-class", choices=["HP", "Spot"], required=True)
    philly_parser.add_argument("--trace-id", default="microsoft-philly")

    alibaba_parser = subparsers.add_parser(
        "import-alibaba", help="convert Alibaba node/job tables into the canonical trace contract"
    )
    alibaba_parser.add_argument("--node-info", required=True)
    alibaba_parser.add_argument("--job-info", required=True)
    alibaba_parser.add_argument("--output-dir", required=True)
    alibaba_parser.add_argument("--trace-id", default="alibaba-spot-gpu")
    alibaba_parser.add_argument("--time-origin", default="2024-03-01 00:00:00")
    alibaba_parser.add_argument("--gpu-model", action="append", dest="gpu_models")
    alibaba_parser.add_argument("--max-submit-time-seconds", type=float)
    alibaba_parser.add_argument("--evaluation-start-seconds", type=float)
    alibaba_parser.add_argument("--evaluation-end-seconds", type=float)
    alibaba_parser.add_argument(
        "--warmup-start-seconds",
        type=float,
        help="optional bounded carry-in start; requires an evaluation window",
    )

    alibaba_v2023_parser = subparsers.add_parser(
        "import-alibaba-v2023",
        help="convert Alibaba v2023 GPU/QoS tables into the canonical trace contract",
    )
    alibaba_v2023_parser.add_argument("--node-info", required=True)
    alibaba_v2023_parser.add_argument("--pod-info", required=True)
    alibaba_v2023_parser.add_argument("--output-dir", required=True)
    alibaba_v2023_parser.add_argument("--trace-id", default="alibaba-gpu-v2023-qos")
    alibaba_v2023_parser.add_argument(
        "--phase",
        action="append",
        dest="phases",
        choices=["Running", "Failed", "Succeeded"],
    )

    workload_parser = subparsers.add_parser(
        "analyze-trace", help="analyze any canonical SchedNav trace"
    )
    workload_parser.add_argument("--trace", required=True)
    workload_parser.add_argument("--evaluation-start-seconds", type=float)
    workload_parser.add_argument("--evaluation-end-seconds", type=float)
    workload_parser.add_argument("--sample-interval-seconds", type=int, default=3600)
    workload_parser.add_argument("--output", required=True)

    slice_parser = subparsers.add_parser(
        "slice-trace", help="create an origin-preserving prefix of a canonical trace"
    )
    slice_parser.add_argument("--trace", required=True)
    slice_parser.add_argument("--output-dir", required=True)
    slice_parser.add_argument("--trace-id", required=True)
    slice_parser.add_argument("--max-submit-time-seconds", type=float, required=True)
    slice_parser.add_argument("--gpu-model", action="append", dest="gpu_models")

    simulate_parser = subparsers.add_parser(
        "simulate", help="run the built-in deterministic simulator"
    )
    simulate_parser.add_argument("--trace", required=True)
    simulate_parser.add_argument("--policy", required=True)
    simulate_parser.add_argument("--result", required=True)
    simulate_parser.add_argument("--metrics", required=True)

    compare_parser = subparsers.add_parser(
        "compare-policies", help="compare two canonical metrics reports without selecting a winner"
    )
    compare_parser.add_argument("--left", required=True)
    compare_parser.add_argument("--right", required=True)
    compare_parser.add_argument("--output", required=True)

    portfolio_parser = subparsers.add_parser(
        "compare-portfolio", help="compare three to five canonical metrics reports without ranking"
    )
    portfolio_parser.add_argument("--metrics", nargs="+", required=True)
    portfolio_parser.add_argument("--output", required=True)

    slo_parser = subparsers.add_parser(
        "audit-slo", help="apply explicit deterministic SLO constraints"
    )
    slo_parser.add_argument("--metrics", required=True)
    slo_parser.add_argument("--slo", required=True)
    slo_parser.add_argument("--baseline", help="same-trace FIFO metrics for relative thresholds")
    slo_parser.add_argument("--output", required=True)

    rank_parser = subparsers.add_parser(
        "rank-policies", help="apply the declared hard-SLO-first hierarchical ranking"
    )
    rank_parser.add_argument("--metrics", nargs="+", required=True)
    rank_parser.add_argument("--audits", nargs="+", required=True)
    rank_parser.add_argument("--slo", required=True)
    rank_parser.add_argument("--output", required=True)

    args = parser.parse_args()
    result: dict[str, Any]
    exit_code = 0

    if args.command == "validate-trace":
        trace = load_canonical_trace(_path(args.trace))
        result = {
            "valid": True,
            "trace_id": trace.trace_id,
            "trace_fingerprint": trace.fingerprint,
            "node_count": len(trace.nodes),
            "job_count": len(trace.jobs),
            "capacity_gpus": trace.capacity_gpus,
        }
    elif args.command == "import-philly":
        manifest = import_philly_trace(
            _path(args.job_log),
            _path(args.machine_list),
            _path(args.output_dir),
            service_class=args.service_class,
            trace_id=args.trace_id,
        )
        trace = load_canonical_trace(manifest)
        result = {
            "trace_manifest": str(manifest),
            "trace_id": trace.trace_id,
            "trace_fingerprint": trace.fingerprint,
            "job_count": len(trace.jobs),
        }
    elif args.command == "import-alibaba":
        manifest = import_alibaba_trace(
            _path(args.node_info),
            _path(args.job_info),
            _path(args.output_dir),
            trace_id=args.trace_id,
            time_origin=args.time_origin,
            gpu_models=set(args.gpu_models) if args.gpu_models else None,
            max_submit_time_seconds=args.max_submit_time_seconds,
            evaluation_start_seconds=args.evaluation_start_seconds,
            evaluation_end_seconds=args.evaluation_end_seconds,
            warmup_start_seconds=args.warmup_start_seconds,
        )
        trace = load_canonical_trace(manifest)
        result = {
            "trace_manifest": str(manifest),
            "trace_id": trace.trace_id,
            "trace_fingerprint": trace.fingerprint,
            "job_count": len(trace.jobs),
        }
    elif args.command == "import-alibaba-v2023":
        manifest = import_alibaba_gpu_v2023_trace(
            _path(args.node_info),
            _path(args.pod_info),
            _path(args.output_dir),
            trace_id=args.trace_id,
            included_phases=set(args.phases) if args.phases else None,
        )
        trace = load_canonical_trace(manifest)
        result = {
            "trace_manifest": str(manifest),
            "trace_id": trace.trace_id,
            "trace_fingerprint": trace.fingerprint,
            "job_count": len(trace.jobs),
        }
    elif args.command == "analyze-trace":
        result = analyze_trace_file(
            _path(args.trace),
            evaluation_start_seconds=args.evaluation_start_seconds,
            evaluation_end_seconds=args.evaluation_end_seconds,
            sample_interval_seconds=args.sample_interval_seconds,
        )
        _write_json(_path(args.output), result)
    elif args.command == "slice-trace":
        manifest = slice_canonical_trace(
            load_canonical_trace(_path(args.trace)),
            _path(args.output_dir),
            trace_id=args.trace_id,
            max_submit_time_seconds=args.max_submit_time_seconds,
            gpu_models=set(args.gpu_models) if args.gpu_models else None,
        )
        trace = load_canonical_trace(manifest)
        result = {
            "trace_manifest": str(manifest),
            "trace_id": trace.trace_id,
            "trace_fingerprint": trace.fingerprint,
            "job_count": len(trace.jobs),
        }
    elif args.command == "simulate":
        simulation_result, metrics = run_native_simulation(
            _path(args.trace), _path(args.policy)
        )
        result_path = _path(args.result)
        metrics_path = _path(args.metrics)
        _write_json(result_path, simulation_result)
        _write_json(metrics_path, metrics)
        result = {
            "result": str(result_path),
            "metrics": str(metrics_path),
            "result_fingerprint": simulation_result["result_fingerprint"],
            "metrics_fingerprint": metrics["metrics_fingerprint"],
        }
    elif args.command == "compare-policies":
        result = compare_policy_metrics(_path(args.left), _path(args.right))
        _write_json(_path(args.output), result)
        exit_code = 0 if result["comparable"] else 1
    elif args.command == "compare-portfolio":
        result = compare_policy_portfolio([_path(path) for path in args.metrics])
        _write_json(_path(args.output), result)
        exit_code = 0 if result["comparable"] else 1
    elif args.command == "audit-slo":
        result = audit_slo(
            _path(args.metrics),
            _path(args.slo),
            _path(args.baseline) if args.baseline else None,
        )
        _write_json(_path(args.output), result)
        exit_code = 0 if result["audit_passed"] else 1
    else:
        result = rank_audited_policies(
            [_path(path) for path in args.metrics],
            [_path(path) for path in args.audits],
            _path(args.slo),
        )
        _write_json(_path(args.output), result)

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
