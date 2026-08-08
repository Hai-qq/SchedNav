"""Command-line entry point for deterministic GFS reproduction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .action_space import materialize_policy_action
from .contracts import RunSpec
from .eviction_gate import evaluate_eviction_gate
from .gfs_adapter import compare_run_manifests, prepare_trace, run_reproduction, verify_local_inputs
from .metrics import extract_metrics
from .policy_compare import compare_policy_metrics
from .policy_portfolio import compare_policy_portfolio
from .policy_rank import rank_audited_policies
from .slo import audit_slo
from .window_scan import scan_eviction_candidates
from .workload import analyze_workload


def _load(config: str) -> RunSpec:
    return RunSpec.load(Path(config).resolve())


def main() -> int:
    parser = argparse.ArgumentParser(description="SchedNav GFS reproduction gate")
    parser.add_argument("--project-root", default=".", help="SchedNav project root")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate config and pinned local inputs")
    validate_parser.add_argument("--config", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="prepare an ignored local golden trace")
    prepare_parser.add_argument("--config", required=True)

    run_parser = subparsers.add_parser("run", help="run one isolated GFS replicate")
    run_parser.add_argument("--config", required=True)
    run_parser.add_argument("--replicate", required=True)

    compare_parser = subparsers.add_parser("compare", help="compare deterministic CSV evidence")
    compare_parser.add_argument("--first", required=True)
    compare_parser.add_argument("--second", required=True)
    compare_parser.add_argument("--output")

    metrics_parser = subparsers.add_parser("metrics", help="extract canonical metrics from one succeeded run")
    metrics_parser.add_argument("--config", required=True)
    metrics_parser.add_argument("--manifest", required=True)
    metrics_parser.add_argument("--output", required=True)

    scan_parser = subparsers.add_parser("scan-windows", help="rank real-trace eviction candidates")
    scan_parser.add_argument("--trace-dir", required=True)
    scan_parser.add_argument("--earliest-date", required=True)
    scan_parser.add_argument("--latest-date", required=True)
    scan_parser.add_argument("--limit", type=int, default=20)
    scan_parser.add_argument("--output", required=True)

    gate_parser = subparsers.add_parser("eviction-gate", help="require CSV-derived Spot preemption evidence")
    gate_parser.add_argument("--metrics", required=True)
    gate_parser.add_argument("--output", required=True)

    policy_compare_parser = subparsers.add_parser(
        "compare-policies", help="compare two canonical metrics reports without selecting a winner"
    )
    policy_compare_parser.add_argument("--left", required=True)
    policy_compare_parser.add_argument("--right", required=True)
    policy_compare_parser.add_argument("--output", required=True)

    portfolio_parser = subparsers.add_parser(
        "compare-portfolio", help="compare three to five canonical metrics reports without ranking them"
    )
    portfolio_parser.add_argument("--metrics", nargs="+", required=True)
    portfolio_parser.add_argument("--output", required=True)

    rank_parser = subparsers.add_parser(
        "rank-policies", help="apply the declared hard-SLO-first hierarchical ranking"
    )
    rank_parser.add_argument("--metrics", nargs="+", required=True)
    rank_parser.add_argument("--audits", nargs="+", required=True)
    rank_parser.add_argument("--slo", required=True)
    rank_parser.add_argument("--output", required=True)

    slo_parser = subparsers.add_parser("audit-slo", help="apply explicit deterministic SLO constraints")
    slo_parser.add_argument("--metrics", required=True)
    slo_parser.add_argument("--slo", required=True)
    slo_parser.add_argument("--baseline", help="canonical FIFO metrics for relative thresholds")
    slo_parser.add_argument("--output", required=True)

    materialize_parser = subparsers.add_parser(
        "materialize-policy", help="convert a bounded high-level action into an executable GFS run spec"
    )
    materialize_parser.add_argument("--base-config", required=True)
    materialize_parser.add_argument("--action-space", required=True)
    materialize_parser.add_argument("--action", required=True)
    materialize_parser.add_argument("--output", required=True)
    materialize_parser.add_argument("--receipt", required=True)

    workload_parser = subparsers.add_parser("analyze-workload", help="summarize one real trace window")
    workload_parser.add_argument("--trace-dir", required=True)
    workload_parser.add_argument("--gpu-model", required=True)
    workload_parser.add_argument("--evaluation-start", required=True)
    workload_parser.add_argument("--evaluation-end", required=True)
    workload_parser.add_argument("--sample-interval-seconds", type=int, default=3600)
    workload_parser.add_argument("--output", required=True)

    args = parser.parse_args()
    project_root = Path(args.project_root).resolve()
    if args.command == "validate":
        spec = _load(args.config)
        paths = verify_local_inputs(spec, project_root)
        result = {"valid": True, "run_spec_fingerprint": spec.fingerprint, "paths": {k: str(v) for k, v in paths.items()}}
    elif args.command == "prepare":
        prepared_dir, manifest = prepare_trace(_load(args.config), project_root)
        result = {"prepared_dir": str(prepared_dir), "manifest": manifest}
    elif args.command == "run":
        run_dir, manifest = run_reproduction(_load(args.config), project_root, args.replicate)
        result = {"run_dir": str(run_dir), "manifest": manifest}
    elif args.command == "compare":
        result = compare_run_manifests(Path(args.first).resolve(), Path(args.second).resolve())
        if args.output:
            Path(args.output).resolve().write_text(
                json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
    elif args.command == "metrics":
        result = extract_metrics(_load(args.config), Path(args.manifest).resolve())
        Path(args.output).resolve().write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif args.command == "scan-windows":
        result = scan_eviction_candidates(
            Path(args.trace_dir).resolve(), args.earliest_date, args.latest_date, args.limit
        )
        Path(args.output).resolve().write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif args.command == "eviction-gate":
        result = evaluate_eviction_gate(Path(args.metrics).resolve())
        Path(args.output).resolve().write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif args.command == "compare-policies":
        result = compare_policy_metrics(Path(args.left).resolve(), Path(args.right).resolve())
        Path(args.output).resolve().write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif args.command == "compare-portfolio":
        result = compare_policy_portfolio([Path(path).resolve() for path in args.metrics])
        Path(args.output).resolve().write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif args.command == "rank-policies":
        result = rank_audited_policies(
            [Path(path).resolve() for path in args.metrics],
            [Path(path).resolve() for path in args.audits],
            Path(args.slo).resolve(),
        )
        Path(args.output).resolve().write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif args.command == "audit-slo":
        result = audit_slo(
            Path(args.metrics).resolve(),
            Path(args.slo).resolve(),
            Path(args.baseline).resolve() if args.baseline else None,
        )
        Path(args.output).resolve().write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif args.command == "materialize-policy":
        run_spec, result = materialize_policy_action(
            Path(args.base_config).resolve(),
            Path(args.action_space).resolve(),
            Path(args.action).resolve(),
        )
        Path(args.output).resolve().write_text(
            json.dumps(run_spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        Path(args.receipt).resolve().write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    else:
        result = analyze_workload(
            Path(args.trace_dir).resolve(),
            args.gpu_model,
            args.evaluation_start,
            args.evaluation_end,
            args.sample_interval_seconds,
        )
        Path(args.output).resolve().write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if args.command == "compare":
        return 0 if result["deterministic_match"] else 1
    if args.command == "eviction-gate":
        return 0 if result["gate_passed"] else 1
    if args.command in {"compare-policies", "compare-portfolio"}:
        return 0 if result["comparable"] else 1
    if args.command == "audit-slo":
        return 0 if result["audit_passed"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
