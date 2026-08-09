"""Publish a compact content-addressed adaptive holdout receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT_DEFAULT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from schednav.adaptive_benchmark import build_adaptive_evidence


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--agent-controller", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite output: {output}")
    receipt = build_adaptive_evidence(
        _load(Path(args.design).resolve()),
        _load(Path(args.benchmark).resolve()),
        _load(Path(args.agent_controller).resolve()),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "receipt_fingerprint": receipt["receipt_fingerprint"],
                "evaluation_window_count": len(receipt["evaluation_windows"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
