from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
import zipfile

from scripts.build_agentteams_bundle import REQUIRED_MODEL_ID, build_packages, render_resources


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AgentTeamsBundleTests(unittest.TestCase):
    def test_identity_list_and_package_manifest_cover_every_role(self):
        identities = json.loads(
            (PROJECT_ROOT / "configs" / "agentteams" / "agent-identities.json").read_text(
                encoding="utf-8"
            )
        )
        package_manifest = json.loads(
            (PROJECT_ROOT / "integrations" / "agentteams" / "package-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        worker_ids = {worker["id"] for worker in identities["workers"]}
        self.assertEqual(
            worker_ids,
            {"workload-analyst", "scheduling-strategist", "simulation-agent", "slo-auditor"},
        )
        self.assertEqual(set(package_manifest["packages"]), worker_ids | {"schednav-manager"})
        assigned_skills = {
            skill
            for skills in package_manifest["packages"].values()
            for skill in skills
        }
        for skill in assigned_skills:
            self.assertTrue((PROJECT_ROOT / ".codex" / "skills" / skill / "SKILL.md").is_file())

    def test_builds_deterministic_skill_packages_and_resources(self):
        with TemporaryDirectory() as first_temp, TemporaryDirectory() as second_temp:
            first = Path(first_temp) / "bundle"
            second = Path(second_temp) / "bundle"
            manifest = PROJECT_ROOT / "integrations" / "agentteams" / "package-manifest.json"
            first_receipt = build_packages(PROJECT_ROOT, manifest, first)
            second_receipt = build_packages(PROJECT_ROOT, manifest, second)
            self.assertEqual(first_receipt, second_receipt)
            self.assertEqual(len(first_receipt["packages"]), 5)
            with zipfile.ZipFile(first / "simulation-agent.zip") as archive:
                self.assertIn("manifest.json", archive.namelist())
                self.assertIn("skills/simulate-gfs-policy/SKILL.md", archive.namelist())
                package_metadata = json.loads(archive.read("manifest.json"))
                self.assertEqual(package_metadata["worker"]["model"], REQUIRED_MODEL_ID)
                self.assertEqual(package_metadata["worker"]["runtime"], "copaw")
            rendered = first / "schednav-resources.yaml"
            render_resources(
                PROJECT_ROOT / "integrations" / "agentteams" / "schednav-resources.yaml.example",
                rendered,
                REQUIRED_MODEL_ID,
            )
            content = rendered.read_text(encoding="utf-8")
            self.assertNotIn("REPLACE_WITH_MODEL_ID", content)
            self.assertEqual(content.count(f"model: {REQUIRED_MODEL_ID}"), 5)
            self.assertEqual(content.count("runtime: copaw"), 5)
            self.assertEqual(content.count("url: http://host.docker.internal:18765/mcp"), 5)
            self.assertIn("kind: Manager\nmetadata:\n  name: default", content)
            self.assertNotIn("name: schednav-manager\nspec:", content)
            self.assertNotIn("package: file://", content)

    def test_rejects_every_model_except_deepseek_v4_flash(self):
        with TemporaryDirectory() as temp:
            rendered = Path(temp) / "schednav-resources.yaml"
            with self.assertRaisesRegex(ValueError, "locked to deepseek-v4-flash"):
                render_resources(
                    PROJECT_ROOT
                    / "integrations"
                    / "agentteams"
                    / "schednav-resources.yaml.example",
                    rendered,
                    "deepseek-v4-pro",
                )


if __name__ == "__main__":
    unittest.main()
