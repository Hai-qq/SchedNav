import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from schednav.contracts import RunSpec
from schednav.gfs_adapter import (
    GFS_VENDOR_MANIFEST,
    GFS_VENDOR_SCHEMA,
    TRACE_VENDOR_MANIFEST,
    TRACE_VENDOR_SCHEMA,
    compare_run_manifests,
    sha256_file,
    verify_local_inputs,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "configs" / "baselines" / "golden-a800-2024-04-07.json"


class AdapterTests(unittest.TestCase):
    def _vendored_spec(self, root: Path) -> RunSpec:
        value = json.loads(
            (PROJECT_ROOT / "configs" / "baselines" / "stress-gpu-series-2-2024-04-12.json").read_text(
                encoding="utf-8"
            )
        )
        value.update(
            {
                "gfs_dir": "26ASPLOS-Spot",
                "gfs_patch": "patches/gfs/reproduction-gate.patch",
                "source_trace_dir": "clusterdata/cluster-trace-v2026-spot-gpu",
                "python_executable": ".venv-gfs/Scripts/python.exe",
                "artifacts_dir": "artifacts/reproduction",
            }
        )
        gfs = root / value["gfs_dir"]
        trace = root / value["source_trace_dir"]
        patch = root / value["gfs_patch"]
        python = root / value["python_executable"]
        for path in (gfs, trace, patch.parent, python.parent):
            path.mkdir(parents=True, exist_ok=True)
        simulator = gfs / "simulator.py"
        simulator.write_text(
            "\n".join(
                (
                    "--trace-start",
                    "--trace-end",
                    "--log-start",
                    "--log-end",
                    "--embed",
                    "--seed",
                    "--deterministic",
                    "--cpu",
                )
            ),
            encoding="utf-8",
        )
        requirements = gfs / "requirements.txt"
        requirements.write_text("numpy==2.0.2\n", encoding="utf-8")
        patch.write_text("test patch\n", encoding="utf-8")
        python.write_bytes(b"test interpreter")
        node = trace / "node_info_df.csv"
        job = trace / "job_info_df.csv"
        node.write_text("node_name\nnode-a\n", encoding="utf-8")
        job.write_text("job_name\njob-a\n", encoding="utf-8")
        (gfs / GFS_VENDOR_MANIFEST).write_text(
            json.dumps(
                {
                    "schema_version": GFS_VENDOR_SCHEMA,
                    "upstream_commit": value["gfs_commit"],
                    "compatibility_patch_sha256": sha256_file(patch),
                    "files": {
                        name: {"sha256": sha256_file(gfs / name), "size_bytes": (gfs / name).stat().st_size}
                        for name in ("simulator.py", "requirements.txt")
                    },
                }
            ),
            encoding="utf-8",
        )
        (trace / TRACE_VENDOR_MANIFEST).write_text(
            json.dumps(
                {
                    "schema_version": TRACE_VENDOR_SCHEMA,
                    "upstream_commit": value["trace_commit"],
                    "files": {
                        name: {
                            "sha256": sha256_file(trace / name),
                            "size_bytes": (trace / name).stat().st_size,
                        }
                        for name in ("node_info_df.csv", "job_info_df.csv")
                    },
                }
            ),
            encoding="utf-8",
        )
        return RunSpec.from_dict(value)

    def test_compare_detects_matching_csv_evidence(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common = {
                "status": "succeeded",
                "run_spec_fingerprint": "a",
                "policy_fingerprint": "b",
                "trace_id": "c",
                "gfs_patch_sha256": "patch",
                "result_files": [{"path": "cluster/out.csv", "sha256": "d"}],
            }
            first = {**common, "run_id": "r1"}
            second = {**common, "run_id": "r2"}
            first_path = root / "first.json"
            second_path = root / "second.json"
            first_path.write_text(json.dumps(first), encoding="utf-8")
            second_path.write_text(json.dumps(second), encoding="utf-8")
            comparison = compare_run_manifests(first_path, second_path)
            self.assertTrue(comparison["deterministic_match"])

    def test_sha256_file_is_stable(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "evidence.csv"
            path.write_text("a,b\n1,2\n", encoding="utf-8")
            self.assertEqual(sha256_file(path), sha256_file(path))

    def test_vendored_inputs_are_accepted_without_git_metadata(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = self._vendored_spec(root)
            paths = verify_local_inputs(spec, root)
            self.assertEqual(paths["gfs_dir"], (root / "26ASPLOS-Spot").resolve())

    def test_vendored_input_tampering_is_rejected(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = self._vendored_spec(root)
            (root / "26ASPLOS-Spot" / "simulator.py").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "failed attestation"):
                verify_local_inputs(spec, root)


if __name__ == "__main__":
    unittest.main()
