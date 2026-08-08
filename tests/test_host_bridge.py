from pathlib import Path
from tempfile import TemporaryDirectory
import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from schednav.host_bridge import (
    BridgeCatalog,
    BridgeHTTPServer,
    BridgeRequestError,
    BridgeService,
    IdempotencyConflict,
    REQUEST_SCHEMA,
)


class HostBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.project_root = Path(self.temporary.name)
        for relative in (
            "configs/run.json",
            "configs/action-space.json",
            "configs/action.json",
            "configs/slo.json",
            "artifacts/baseline.json",
            "artifacts/input.json",
        ):
            path = self.project_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        config = {
            "schema_version": "schednav.host-bridge-config/v1",
            "artifact_root": "artifacts",
            "task_subdir": "agentteams-bridge/tasks",
            "max_workers": 1,
            "run_configs": {"window-a": "configs/run.json"},
            "action_space": "configs/action-space.json",
            "actions": {"policy-a": "configs/action.json"},
            "slo_specs": {"slo-a": "configs/slo.json"},
            "baseline_metrics": {"baseline-a": "artifacts/baseline.json"},
        }
        self.config_path = self.project_root / "configs/bridge.json"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.catalog = BridgeCatalog.load(self.project_root, self.config_path)

    def tearDown(self):
        self.temporary.cleanup()

    def _request(self, sample: int = 3600):
        return {
            "schema_version": REQUEST_SCHEMA,
            "operation": "analyze_workload",
            "arguments": {
                "run_config_id": "window-a",
                "sample_interval_seconds": sample,
            },
        }

    def test_idempotent_submission_returns_the_same_task(self):
        def handler(_arguments, task_dir, _task_id):
            result = task_dir / "result.json"
            result.write_text("{}\n", encoding="utf-8")
            return {"result": self.catalog.artifact_ref(result)}

        service = BridgeService(self.catalog, {"analyze_workload": handler})
        try:
            first, created = service.submit(self._request(), "bridge-test-0001")
            second, created_again = service.submit(self._request(), "bridge-test-0001")
            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(first["task_id"], second["task_id"])
            for _ in range(100):
                completed = service.get_task(first["task_id"])
                if completed["status"] == "succeeded":
                    break
                time.sleep(0.01)
            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(completed["artifacts"]["result"].split("/")[-1], "result.json")
        finally:
            service.close()

    def test_idempotency_key_conflicts_on_different_request(self):
        service = BridgeService(
            self.catalog,
            {"analyze_workload": lambda _arguments, _task_dir, _task_id: {}},
        )
        try:
            service.submit(self._request(3600), "bridge-test-0002")
            with self.assertRaises(IdempotencyConflict):
                service.submit(self._request(7200), "bridge-test-0002")
        finally:
            service.close()

    def test_artifact_reference_cannot_escape_root(self):
        with self.assertRaises(BridgeRequestError):
            self.catalog.resolve_artifact_ref("../configs/run.json")
        with self.assertRaises(BridgeRequestError):
            self.catalog.resolve_artifact_ref("C:\\Windows\\win.ini")
        resolved = self.catalog.resolve_artifact_ref("input.json")
        self.assertEqual(resolved, (self.project_root / "artifacts/input.json").resolve())

    def test_catalog_rejects_project_escape(self):
        value = json.loads(self.config_path.read_text(encoding="utf-8"))
        value["actions"]["policy-a"] = "../outside.json"
        self.config_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(BridgeRequestError, "inside the project root"):
            BridgeCatalog.load(self.project_root, self.config_path)

    def test_operation_contract_rejects_unlisted_arguments(self):
        service = BridgeService(
            self.catalog,
            {"analyze_workload": lambda _arguments, _task_dir, _task_id: {}},
        )
        request = self._request()
        request["arguments"]["shell"] = "whoami"
        try:
            with self.assertRaisesRegex(BridgeRequestError, "unexpected"):
                service.submit(request, "bridge-test-0003")
        finally:
            service.close()

    def test_simulation_rejects_unlisted_action_profile(self):
        service = BridgeService(self.catalog)
        try:
            with self.assertRaisesRegex(BridgeRequestError, "Unknown action_id"):
                service.submit(
                    {
                        "schema_version": REQUEST_SCHEMA,
                        "operation": "simulate_policy",
                        "arguments": {
                            "run_config_id": "window-a",
                            "action_id": "unlisted-action",
                        },
                    },
                    "bridge-test-0004",
                )
        finally:
            service.close()

    def test_audit_rejects_unknown_slo_spec(self):
        service = BridgeService(self.catalog)
        try:
            with self.assertRaisesRegex(BridgeRequestError, "Unknown slo_spec_id"):
                service.submit(
                    {
                        "schema_version": REQUEST_SCHEMA,
                        "operation": "audit_slo",
                        "arguments": {
                            "metrics_ref": "input.json",
                            "slo_spec_id": "missing-slo",
                            "baseline_metrics_id": "baseline-a",
                        },
                    },
                    "bridge-test-0005",
                )
        finally:
            service.close()

    def test_mcp_lists_and_calls_bounded_tools_with_bearer_auth(self):
        service = BridgeService(
            self.catalog,
            {"analyze_workload": lambda _arguments, _task_dir, _task_id: {}},
        )
        token = "x" * 32
        server = BridgeHTTPServer(("127.0.0.1", 0), service, (token,))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/mcp"

        def call(payload, bearer_token=token):
            headers = {"Content-Type": "application/json"}
            if bearer_token is not None:
                headers["Authorization"] = f"Bearer {bearer_token}"
            request = Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))

        try:
            with self.assertRaises(HTTPError) as unauthorized:
                call({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, None)
            self.assertEqual(unauthorized.exception.code, 401)
            with self.assertRaises(HTTPError) as invalid_token:
                call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "y" * 32)
            self.assertEqual(invalid_token.exception.code, 401)
            status, listed = call({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
            self.assertEqual(status, 200)
            names = {tool["name"] for tool in listed["result"]["tools"]}
            self.assertEqual(
                names,
                {
                    "analyze_workload",
                    "simulate_policy",
                    "compare_policies",
                    "audit_slo",
                    "rank_policies",
                    "get_task",
                    "read_artifact",
                },
            )
            status, called = call(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "analyze_workload",
                        "arguments": {
                            "idempotency_key": "mcp-call-0001",
                            "run_config_id": "window-a",
                        },
                    },
                }
            )
            self.assertEqual(status, 200)
            self.assertFalse(called["result"]["isError"])
            self.assertRegex(called["result"]["structuredContent"]["task_id"], "^[0-9a-f]{32}$")

            readable = self.project_root / "artifacts/readable.json"
            readable.write_text(
                json.dumps(
                    {
                        "schema_version": "schednav.workload-summary/v1",
                        "workload_fingerprint": "a" * 64,
                    }
                ),
                encoding="utf-8",
            )
            _status, read_result = call(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "read_artifact",
                        "arguments": {"artifact_ref": "readable.json"},
                    },
                }
            )
            self.assertFalse(read_result["result"]["isError"])
            self.assertEqual(
                read_result["result"]["structuredContent"]["schema_version"],
                "schednav.workload-summary/v1",
            )

            blocked = self.project_root / "artifacts/blocked.json"
            blocked.write_text(
                json.dumps({"schema_version": "schednav.bridge-failure/v1"}),
                encoding="utf-8",
            )
            _status, blocked_result = call(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "read_artifact",
                        "arguments": {"artifact_ref": "blocked.json"},
                    },
                }
            )
            self.assertTrue(blocked_result["result"]["isError"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            service.close()


if __name__ == "__main__":
    unittest.main()
