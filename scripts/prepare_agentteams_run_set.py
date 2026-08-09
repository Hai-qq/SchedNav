"""Stage a completed multi-window trace set for bounded AgentTeams execution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
from typing import Any


PROJECT_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT_DEFAULT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from schednav.contracts import canonical_sha256
from schednav.host_bridge import BridgeCatalog


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def prepare_run_set(
    project_root: Path,
    experiment_root: Path,
    output_config: Path,
    *,
    run_set_id: str,
    action_space_relative: str,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    experiment_root = experiment_root.resolve()
    output_config = output_config.resolve()
    if output_config.exists():
        raise FileExistsError(f"Refusing to overwrite bridge config: {output_config}")
    try:
        output_config.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("The generated bridge config must stay inside the project") from exc
    manifest = json.loads(
        (experiment_root / "experiment-manifest.json").read_text(encoding="utf-8")
    )
    summary = json.loads(
        (experiment_root / "multiwindow-summary.json").read_text(encoding="utf-8")
    )
    action_space_path = (project_root / action_space_relative).resolve()
    action_space = json.loads(action_space_path.read_text(encoding="utf-8"))
    if manifest["multiwindow_fingerprint"] != summary["multiwindow_fingerprint"]:
        raise ValueError("Experiment manifest and summary fingerprints do not match")
    if manifest["action_space"]["fingerprint"] != canonical_sha256(action_space):
        raise ValueError("Requested action space does not match the experiment")

    trace_root = project_root / "datasets" / "local" / "agentteams" / run_set_id
    run_config_root = output_config.parent / "run-configs"
    run_configs: dict[str, str] = {}
    run_config_ids: list[str] = []
    for window in sorted(summary["windows"], key=lambda item: item["date"]):
        date = str(window["date"])
        run_config_id = f"window-{date}"
        source_trace_root = experiment_root / "windows" / date / "trace"
        target_trace_root = trace_root / date
        target_trace_root.mkdir(parents=True, exist_ok=False)
        for filename in ("trace.json", "nodes.csv", "jobs.csv"):
            shutil.copy2(source_trace_root / filename, target_trace_root / filename)
        run_config_path = run_config_root / f"{run_config_id}.json"
        _write_json(
            run_config_path,
            {
                "schema_version": "schednav.native-run-config/v1",
                "trace_manifest": (target_trace_root / "trace.json")
                .relative_to(project_root)
                .as_posix(),
            },
        )
        run_configs[run_config_id] = run_config_path.relative_to(project_root).as_posix()
        run_config_ids.append(run_config_id)

    actions: dict[str, str] = {}
    for relative in action_space["profiles"]:
        policy_path = (project_root / relative).resolve()
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        actions[str(policy["action_id"])] = policy_path.relative_to(project_root).as_posix()
    config = {
        "schema_version": "schednav.host-bridge-config/v1",
        "artifact_root": "artifacts",
        "task_subdir": f"agentteams-run-sets/{run_set_id}/tasks",
        "max_workers": 1,
        "run_configs": run_configs,
        "run_sets": {run_set_id: run_config_ids},
        "action_space": action_space_path.relative_to(project_root).as_posix(),
        "actions": actions,
        "slo_specs": {
            "schednav-demo-slo-v1": "configs/slos/schednav-demo-slo-v1.json"
        },
        "baseline_metrics": {},
    }
    _write_json(output_config, config)
    BridgeCatalog.load(project_root, output_config)
    return {
        "bridge_config": str(output_config),
        "run_set_id": run_set_id,
        "window_count": len(run_config_ids),
        "action_ids": list(actions),
        "multiwindow_fingerprint": summary["multiwindow_fingerprint"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(PROJECT_ROOT_DEFAULT))
    parser.add_argument("--experiment-directory", required=True)
    parser.add_argument("--run-set-id", default="alibaba-v2-12d")
    parser.add_argument(
        "--action-space", default="configs/action_spaces/native-multiwindow-v2.json"
    )
    parser.add_argument(
        "--output-config",
        default="artifacts/agentteams-multiwindow/bridge-config.json",
    )
    args = parser.parse_args()
    project_root = Path(args.project_root).resolve()
    output_config = (project_root / args.output_config).resolve()
    result = prepare_run_set(
        project_root,
        Path(args.experiment_directory),
        output_config,
        run_set_id=args.run_set_id,
        action_space_relative=args.action_space,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
