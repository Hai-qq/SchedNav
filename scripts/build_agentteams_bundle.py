"""Build deterministic AgentTeams Worker packages and render the resource template."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SAFE_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
REQUIRED_MODEL_ID = "deepseek-v4-flash"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_zip_file(archive: zipfile.ZipFile, source: Path, target: str) -> None:
    info = zipfile.ZipInfo(target, FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, source.read_bytes())


def _write_zip_bytes(archive: zipfile.ZipFile, content: bytes, target: str) -> None:
    info = zipfile.ZipInfo(target, FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, content)


def build_packages(
    project_root: Path,
    manifest_path: Path,
    output_dir: Path,
    model_id: str = REQUIRED_MODEL_ID,
) -> dict:
    if model_id != REQUIRED_MODEL_ID:
        raise ValueError(f"SchedNav AgentTeams model is locked to {REQUIRED_MODEL_ID}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "schednav.agentteams-packages/v1":
        raise ValueError("Unsupported AgentTeams package manifest")
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite AgentTeams bundle directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)
    receipt = {"schema_version": "schednav.agentteams-bundle/v1", "packages": {}}
    skills_root = project_root / ".codex" / "skills"
    for package_name, skill_names in sorted(manifest["packages"].items()):
        if not SAFE_NAME.fullmatch(package_name) or not skill_names:
            raise ValueError(f"Invalid AgentTeams package declaration: {package_name}")
        package_path = output_dir / f"{package_name}.zip"
        members: list[str] = ["manifest.json"]
        with zipfile.ZipFile(package_path, "w") as archive:
            package_metadata = {
                "version": "1.0",
                "worker": {
                    "suggested_name": package_name,
                    "model": model_id,
                    "runtime": "copaw",
                    "apt_packages": [],
                    "pip_packages": [],
                    "npm_packages": [],
                },
            }
            _write_zip_bytes(
                archive,
                (json.dumps(package_metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                "manifest.json",
            )
            for skill_name in sorted(skill_names):
                if not SAFE_NAME.fullmatch(skill_name):
                    raise ValueError(f"Invalid skill name: {skill_name}")
                skill_dir = (skills_root / skill_name).resolve()
                if skill_dir.parent != skills_root.resolve() or not (skill_dir / "SKILL.md").is_file():
                    raise FileNotFoundError(f"Missing project skill: {skill_name}")
                for source in sorted(path for path in skill_dir.rglob("*") if path.is_file()):
                    relative = source.relative_to(skill_dir).as_posix()
                    target = f"skills/{skill_name}/{relative}"
                    _write_zip_file(archive, source, target)
                    members.append(target)
        receipt["packages"][package_name] = {
            "path": package_path.name,
            "sha256": _sha256(package_path),
            "members": members,
        }
    return receipt


def render_resources(template_path: Path, output_path: Path, model_id: str) -> None:
    if not SAFE_MODEL_ID.fullmatch(model_id):
        raise ValueError("model_id contains characters that are unsafe in the YAML template")
    if model_id != REQUIRED_MODEL_ID:
        raise ValueError(f"SchedNav AgentTeams model is locked to {REQUIRED_MODEL_ID}")
    template = template_path.read_text(encoding="utf-8")
    placeholder = "REPLACE_WITH_MODEL_ID"
    if placeholder not in template:
        raise ValueError("AgentTeams resource template has no model placeholder")
    output_path.write_text(template.replace(placeholder, model_id) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--model-id", default=REQUIRED_MODEL_ID, choices=(REQUIRED_MODEL_ID,))
    parser.add_argument("--output-dir", default="dist/agentteams")
    args = parser.parse_args()
    project_root = Path(args.project_root).resolve()
    output_dir = (project_root / args.output_dir).resolve()
    receipt = build_packages(
        project_root,
        project_root / "integrations" / "agentteams" / "package-manifest.json",
        output_dir,
        args.model_id,
    )
    render_resources(
        project_root / "integrations" / "agentteams" / "schednav-resources.yaml.example",
        output_dir / "schednav-resources.yaml",
        args.model_id,
    )
    receipt_path = output_dir / "bundle-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
