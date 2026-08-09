from pathlib import Path
from tempfile import TemporaryDirectory
import json
import threading
import time
import unittest
from unittest.mock import patch
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
from schednav.native_trace import TraceJob, TraceNode, write_canonical_trace


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
            "run_sets": {"fixture-set": ["window-a"]},
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

    def test_operation_allowlist_removes_legacy_future_visible_tools(self):
        value = json.loads(self.config_path.read_text(encoding="utf-8"))
        value["operation_allowlist"] = ["forecast_demand"]
        self.config_path.write_text(json.dumps(value), encoding="utf-8")
        catalog = BridgeCatalog.load(self.project_root, self.config_path)
        service = BridgeService(catalog)
        try:
            self.assertTrue(service.supports_operation("forecast_demand"))
            self.assertFalse(service.supports_operation("analyze_workload"))
            self.assertFalse(service.supports_operation("simulate_policy"))
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

    def test_native_bridge_runs_the_first_party_simulator(self):
        trace_manifest = write_canonical_trace(
            self.project_root / "datasets/local",
            trace_id="bridge-native",
            time_origin="2026-01-01 00:00:00",
            source={"dataset": "bridge-fixture"},
            nodes=[TraceNode("n1", "A", 2)],
            jobs=[
                TraceJob("spot", 0, 10, 2, "Spot", "A"),
                TraceJob("hp", 2, 2, 2, "HP", "A"),
            ],
        )
        run_config = self.project_root / "configs/native-run.json"
        run_config.write_text(
            json.dumps(
                {
                    "schema_version": "schednav.native-run-config/v1",
                    "trace_manifest": "datasets/local/trace.json",
                }
            ),
            encoding="utf-8",
        )
        policy = self.project_root / "configs/native-policy.json"
        policy.write_text(
            json.dumps(
                {
                    "schema_version": "schednav.simulation-policy/v1",
                    "action_id": "native-policy",
                    "scheduler": "priority_preemptive",
                    "spot_guarantee_seconds": 0,
                    "checkpoint_interval_seconds": 2,
                    "preemption_overhead_seconds": 0,
                    "placement_strategy": "deterministic_best_fit",
                }
            ),
            encoding="utf-8",
        )
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["run_configs"] = {"native-window": "configs/native-run.json"}
        config["run_sets"] = {"native-set": ["native-window"]}
        config["actions"] = {"native-policy": "configs/native-policy.json"}
        config["baseline_metrics"] = {}
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        catalog = BridgeCatalog.load(self.project_root, self.config_path)
        service = BridgeService(catalog)
        try:
            task, _ = service.submit(
                {
                    "schema_version": REQUEST_SCHEMA,
                    "operation": "simulate_policy",
                    "arguments": {
                        "run_config_id": "native-window",
                        "action_id": "native-policy",
                    },
                },
                "native-bridge-0001",
            )
            for _ in range(100):
                completed = service.get_task(task["task_id"])
                if completed["status"] != "queued" and completed["status"] != "running":
                    break
                time.sleep(0.01)
            self.assertEqual(completed["status"], "succeeded")
            metrics = json.loads(
                catalog.resolve_artifact_ref(completed["artifacts"]["metrics"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(metrics["schema_version"], "schednav.metrics-report/v2")
            self.assertEqual(metrics["source"]["engine"]["name"], "schednav-sim")
        finally:
            service.close()

    def test_predictive_tools_emit_cutoff_forecast_and_control_evidence(self):
        write_canonical_trace(
            self.project_root / "datasets/local",
            trace_id="bridge-predictive",
            time_origin="2026-01-01 00:00:00",
            source={"dataset": "bridge-predictive-fixture"},
            nodes=[TraceNode("n1", "A", 4)],
            jobs=[
                TraceJob("spot", 0, 1200, 2, "Spot", "A"),
                TraceJob("hp", 300, 300, 4, "HP", "A"),
            ],
            evaluation_start_seconds=0,
            evaluation_end_seconds=1200,
        )
        run_config = self.project_root / "configs/predictive-run.json"
        run_config.write_text(
            json.dumps(
                {
                    "schema_version": "schednav.native-run-config/v1",
                    "trace_manifest": "datasets/local/trace.json",
                }
            ),
            encoding="utf-8",
        )
        policy = self.project_root / "configs/predictive-policy.json"
        policy.write_text(
            json.dumps(
                {
                    "schema_version": "schednav.simulation-policy/v1",
                    "action_id": "predictive-policy",
                    "scheduler": "priority_preemptive",
                    "spot_guarantee_seconds": 0,
                    "checkpoint_interval_seconds": 300,
                    "preemption_overhead_seconds": 0,
                    "placement_strategy": "deterministic_best_fit",
                }
            ),
            encoding="utf-8",
        )
        controller = self.project_root / "configs/predictive-controller.json"
        controller.write_text(
            json.dumps(
                {
                    "schema_version": "schednav.predictive-controller/v1",
                    "controller_id": "predictive-controller",
                    "model": "seasonal-gaussian-v1",
                    "observation_interval_seconds": 300,
                    "aggregation_interval_seconds": 3600,
                    "lookback_hours": 168,
                    "forecast_horizon_hours": 4,
                    "retrain_interval_seconds": 86400,
                    "guarantee_probability": 0.9,
                    "guarantee_horizons_hours": [1, 2, 4],
                    "minimum_history_hours": 1,
                    "minimum_sigma_gpus": 0.0,
                    "initial_eta": 1.0,
                    "minimum_eta": 0.25,
                    "maximum_eta": 1.25,
                    "feedback_window_seconds": 14400,
                    "starvation_increase_after_seconds": 3600,
                    "high_eviction_ratio": 1.5,
                    "low_eviction_ratio": 0.5,
                    "eta_increase_step": 0.1,
                }
            ),
            encoding="utf-8",
        )
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["run_configs"] = {"predictive-window": "configs/predictive-run.json"}
        config["run_sets"] = {"predictive-set": ["predictive-window"]}
        config["actions"] = {"predictive-policy": "configs/predictive-policy.json"}
        config["controllers"] = {
            "predictive-controller": "configs/predictive-controller.json"
        }
        config["baseline_metrics"] = {}
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        catalog = BridgeCatalog.load(self.project_root, self.config_path)
        service = BridgeService(catalog)

        def completed(request, key):
            task, _ = service.submit(request, key)
            for _ in range(1000):
                current = service.get_task(task["task_id"])
                if current["status"] not in {"queued", "running"}:
                    return current
                time.sleep(0.01)
            self.fail("Bridge task did not finish")

        try:
            forecast = completed(
                {
                    "schema_version": REQUEST_SCHEMA,
                    "operation": "forecast_demand",
                    "arguments": {
                        "run_config_id": "predictive-window",
                        "controller_id": "predictive-controller",
                        "cutoff_seconds": 300,
                    },
                },
                "predictive-forecast-0001",
            )
            self.assertEqual(forecast["status"], "succeeded")
            bundle = json.loads(
                catalog.resolve_artifact_ref(
                    forecast["artifacts"]["predictive_observation"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                bundle["schema_version"],
                "schednav.predictive-observation-bundle/v1",
            )
            simulated = completed(
                {
                    "schema_version": REQUEST_SCHEMA,
                    "operation": "simulate_predictive_policy",
                    "arguments": {
                        "run_config_id": "predictive-window",
                        "action_id": "predictive-policy",
                        "controller_id": "predictive-controller",
                    },
                },
                "predictive-simulate-0001",
            )
            self.assertEqual(simulated["status"], "succeeded")
            metrics = json.loads(
                catalog.resolve_artifact_ref(simulated["artifacts"]["metrics"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                metrics["predictive_control"]["controller_id"],
                "predictive-controller",
            )
        finally:
            service.close()

    def test_run_set_operations_produce_deterministic_multiwindow_evidence(self):
        write_canonical_trace(
            self.project_root / "datasets/local",
            trace_id="bridge-run-set",
            time_origin="2026-01-01 00:00:00",
            source={"dataset": "bridge-fixture"},
            nodes=[TraceNode("n1", "A", 2)],
            jobs=[
                TraceJob("spot", 0, 10, 2, "Spot", "A"),
                TraceJob("hp", 2, 2, 2, "HP", "A"),
            ],
        )
        run_config = self.project_root / "configs/native-run.json"
        run_config.write_text(
            json.dumps(
                {
                    "schema_version": "schednav.native-run-config/v1",
                    "trace_manifest": "datasets/local/trace.json",
                }
            ),
            encoding="utf-8",
        )
        actions = {}
        for action_id, scheduler, delay in (
            ("fifo", "fifo", 0),
            ("preempt", "priority_preemptive", 0),
            ("delayed", "priority_preemptive", 1),
        ):
            path = self.project_root / f"configs/{action_id}.json"
            value = {
                "schema_version": "schednav.simulation-policy/v1",
                "action_id": action_id,
                "scheduler": scheduler,
                "spot_guarantee_seconds": 0,
                "checkpoint_interval_seconds": 2,
                "preemption_overhead_seconds": 0,
                "placement_strategy": "deterministic_best_fit",
            }
            if delay:
                value["hp_preemption_delay_seconds"] = delay
            path.write_text(json.dumps(value), encoding="utf-8")
            actions[action_id] = f"configs/{action_id}.json"
        slo = self.project_root / "configs/run-set-slo.json"
        slo.write_text(
            json.dumps(
                {
                    "schema_version": "schednav.slo-spec/v1",
                    "name": "run-set-test",
                    "constraints": [
                        {
                            "id": "hp-complete",
                            "metric": "hp_completion_rate",
                            "operator": ">=",
                            "threshold": 1.0,
                            "severity": "hard",
                        },
                        {
                            "id": "spot-complete",
                            "metric": "spot_completion_rate",
                            "operator": ">=",
                            "threshold": 1.0,
                            "severity": "hard",
                        },
                    ],
                    "ranking": {
                        "allocation_metric": "allocation_rate_mean",
                        "allocation_tie_band": 0.01,
                        "second_metric": "spot_jct_p95_seconds",
                        "third_metric": "spot_eviction_rate_per_run",
                        "unresolved_tie": "human_approval",
                    },
                }
            ),
            encoding="utf-8",
        )
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["run_configs"] = {"window-a": "configs/native-run.json"}
        config["run_sets"] = {"run-set-a": ["window-a"]}
        config["actions"] = actions
        config["slo_specs"] = {"run-set-slo": "configs/run-set-slo.json"}
        config["baseline_metrics"] = {}
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        catalog = BridgeCatalog.load(self.project_root, self.config_path)
        service = BridgeService(catalog)

        def completed(request, key):
            task, _ = service.submit(request, key)
            for _ in range(1000):
                current = service.get_task(task["task_id"])
                if current["status"] not in {"queued", "running"}:
                    return current
                time.sleep(0.01)
            self.fail("Bridge task did not finish")

        try:
            analyzed = completed(
                {
                    "schema_version": REQUEST_SCHEMA,
                    "operation": "analyze_run_set",
                    "arguments": {"run_set_id": "run-set-a"},
                },
                "run-set-analyze-0001",
            )
            self.assertEqual(analyzed["status"], "succeeded")
            simulated = completed(
                {
                    "schema_version": REQUEST_SCHEMA,
                    "operation": "simulate_run_set",
                    "arguments": {
                        "run_set_id": "run-set-a",
                        "action_ids": ["fifo", "preempt", "delayed"],
                        "repetitions": 2,
                    },
                },
                "run-set-simulate-0001",
            )
            self.assertEqual(simulated["status"], "succeeded")
            simulations_ref = simulated["artifacts"]["run_set_simulations"]
            audited = completed(
                {
                    "schema_version": REQUEST_SCHEMA,
                    "operation": "audit_run_set",
                    "arguments": {
                        "simulations_ref": simulations_ref,
                        "slo_spec_id": "run-set-slo",
                        "baseline_action_id": "fifo",
                    },
                },
                "run-set-audit-0001",
            )
            failure_path = (
                catalog.task_root / audited["task_id"] / "failure.local.json"
            )
            self.assertEqual(
                audited["status"],
                "succeeded",
                failure_path.read_text(encoding="utf-8")
                if failure_path.exists()
                else audited,
            )
            summary = json.loads(
                catalog.resolve_artifact_ref(
                    audited["artifacts"]["multiwindow_summary"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(summary["schema_version"], "schednav.multiwindow-summary/v1")
            self.assertEqual(summary["window_count"], 1)
            self.assertTrue(
                all(
                    policy["deterministic_window_count"] == 1
                    for policy in summary["policies"].values()
                )
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
                    "forecast_demand",
                    "simulate_policy",
                    "simulate_predictive_policy",
                    "compare_policies",
                    "audit_slo",
                    "rank_policies",
                    "analyze_run_set",
                    "simulate_run_set",
                    "audit_run_set",
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
                        "schema_version": "schednav.workload-summary/v2",
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
                "schednav.workload-summary/v2",
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

    def test_delegated_auth_accepts_post_auth_route_error_but_rejects_401(self):
        service = BridgeService(self.catalog, {})
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            service,
            (),
            "http://gateway.test/v1/chat/completions",
        )
        try:
            with patch(
                "schednav.host_bridge.build_opener",
            ) as opener:
                opener.return_value.open.side_effect = HTTPError(
                    "http://gateway.test/v1/chat/completions",
                    404,
                    "authenticated route response",
                    {},
                    None,
                )
                self.assertTrue(server.validate_token("v" * 32))
            with patch(
                "schednav.host_bridge.build_opener",
            ) as opener:
                opener.return_value.open.side_effect = HTTPError(
                    "http://gateway.test/v1/chat/completions",
                    401,
                    "unauthorized",
                    {},
                    None,
                )
                self.assertFalse(server.validate_token("i" * 32))
        finally:
            server.server_close()
            service.close()


if __name__ == "__main__":
    unittest.main()
