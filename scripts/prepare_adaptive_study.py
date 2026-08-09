"""Freeze an all-window adaptive-study design before policy simulation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT_DEFAULT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from schednav.adaptive_benchmark import build_adaptive_design, build_rule_controller
from schednav.contracts import canonical_sha256
from schednav.multiwindow import build_window_selection_report
from schednav.native_trace import import_alibaba_trace, load_canonical_trace


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(PROJECT_ROOT_DEFAULT))
    parser.add_argument("--dataset-directory", required=True)
    parser.add_argument("--output-directory", required=True)
    parser.add_argument(
        "--action-space", default="configs/action_spaces/native-multiwindow-v3.json"
    )
    parser.add_argument("--gpu-model", default="GPU-series-2")
    parser.add_argument("--time-origin", default="2024-03-01 00:00:00")
    parser.add_argument("--calibration-fraction", type=float, default=0.6)
    parser.add_argument("--window-size-seconds", type=int, default=86400)
    parser.add_argument("--sample-interval-seconds", type=int, default=3600)
    parser.add_argument("--min-hp-jobs", type=int, default=20)
    parser.add_argument("--min-spot-jobs", type=int, default=20)
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    dataset_root = Path(args.dataset_directory).resolve()
    output_root = Path(args.output_directory).resolve()
    if output_root.exists():
        raise FileExistsError(f"Refusing to overwrite output directory: {output_root}")
    action_space_path = (project_root / args.action_space).resolve()
    action_space_path.relative_to(project_root)
    action_space = json.loads(action_space_path.read_text(encoding="utf-8"))
    policies = [
        json.loads((project_root / relative).read_text(encoding="utf-8"))
        for relative in action_space["profiles"]
    ]
    output_root.mkdir(parents=True, exist_ok=False)
    trace_path = import_alibaba_trace(
        dataset_root / "node_info_df.csv",
        dataset_root / "job_info_df.csv",
        output_root / "selection-trace",
        trace_id=f"alibaba-{args.gpu_model.lower()}-adaptive-v3-design",
        time_origin=args.time_origin,
        gpu_models={args.gpu_model},
    )
    trace = load_canonical_trace(trace_path)
    selection = build_window_selection_report(
        trace,
        window_size_seconds=args.window_size_seconds,
        sample_interval_seconds=args.sample_interval_seconds,
        min_hp_jobs=args.min_hp_jobs,
        min_spot_jobs=args.min_spot_jobs,
        selection_mode="all-eligible",
    )
    design = build_adaptive_design(
        selection,
        action_space,
        policies,
        action_space_path=args.action_space,
        time_origin=args.time_origin,
        gpu_model=args.gpu_model,
        calibration_fraction=args.calibration_fraction,
    )
    rule = build_rule_controller(design)
    _write(output_root / "window-selection.json", selection)
    _write(output_root / "adaptive-study-design.json", design)
    _write(output_root / "workload-rule-controller.json", rule)
    manifest = {
        "schema_version": "schednav.adaptive-study-preparation/v1",
        "selection_fingerprint": selection["selection_fingerprint"],
        "design_fingerprint": design["design_fingerprint"],
        "rule_controller_fingerprint": rule["controller_fingerprint"],
        "action_space_fingerprint": canonical_sha256(action_space),
    }
    manifest["preparation_fingerprint"] = canonical_sha256(manifest)
    _write(output_root / "preparation-manifest.json", manifest)
    print(
        json.dumps(
            {
                "output_directory": str(output_root),
                "eligible_windows": selection["selected_window_count"],
                "calibration_windows": design["split"]["calibration_window_count"],
                "evaluation_windows": design["split"]["evaluation_window_count"],
                "design_fingerprint": design["design_fingerprint"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
