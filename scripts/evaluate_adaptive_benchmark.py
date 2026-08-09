"""Evaluate frozen adaptive controllers on chronological holdout windows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT_DEFAULT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from schednav.adaptive_benchmark import evaluate_adaptive_benchmark


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-directory", required=True)
    parser.add_argument("--design", required=True)
    parser.add_argument("--rule-controller", required=True)
    parser.add_argument("--agent-controller", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite output: {output}")
    report = evaluate_adaptive_benchmark(
        _load(Path(args.experiment_directory).resolve() / "multiwindow-summary.json"),
        _load(Path(args.design).resolve()),
        _load(Path(args.rule_controller).resolve()),
        _load(Path(args.agent_controller).resolve()),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "benchmark_fingerprint": report["benchmark_fingerprint"],
                "best_static_action_id": report["best_static_action_id"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
