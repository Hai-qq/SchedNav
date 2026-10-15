"""Check tracked and non-ignored files without printing credential values."""
from __future__ import annotations
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_ROOTS = {
    "agentteams", "clusterdata", "artifacts", "dist", ".venv", "venv",
    "data", "datasets", "trace", "traces", "checkpoints", "log", "logs",
}
FORBIDDEN_EXTENSIONS = {".csv", ".zip", ".ckpt", ".pt", ".pth", ".pem", ".key", ".pfx"}
SECRET_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
    r"AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9._-]{24,}|"
    r"api[_-]?key\s*[:=]\s*[A-Za-z0-9._-]{16,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)


def violations_for(relative: str, content: bytes) -> list[str]:
    path = Path(relative)
    reasons = []
    if path.parts[0].lower() in FORBIDDEN_ROOTS:
        reasons.append("forbidden root")
    if path.suffix.lower() in FORBIDDEN_EXTENSIONS or path.name.lower().startswith(".env"):
        reasons.append("forbidden file type/name")
    if len(content) > 2 * 1024 * 1024:
        reasons.append("file exceeds 2 MiB")
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".pdf"}:
        if SECRET_PATTERN.search(content.decode("utf-8", errors="replace")):
            reasons.append("possible credential")
    return reasons


def main() -> int:
    raw = subprocess.check_output([
        "git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    ])
    files = sorted(set(raw.decode("utf-8").rstrip("\0").split("\0")))
    findings = []
    inspected = 0
    for relative in files:
        path = ROOT / relative
        if not path.is_file():
            continue
        inspected += 1
        if path.is_symlink():
            findings.append({"path": relative, "reason": "symlink requires public-boundary review"})
            continue
        findings.extend({"path": relative, "reason": reason}
                        for reason in violations_for(relative, path.read_bytes()))
    for required in ("LICENSE", "third_party/licenses/AgentTeams-Apache-2.0.txt", "third_party/manifest.json"):
        if required not in files or not (ROOT / required).is_file():
            findings.append({"path": required, "reason": "missing license or notice"})
    print(json.dumps({"status": "failed" if findings else "passed", "inspected_files": inspected, "findings": findings}))
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
