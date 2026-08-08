---
name: simulate-gfs-policy
description: Materialize a bounded high-level PolicyAction and execute it in an isolated real GFS simulator process, producing TraceManifest, RunManifest, canonical metrics, and preemption/run/guarantee evidence. Use for SchedNav counterfactual policy evaluation or deterministic replication. Never choose Job-to-node/GPU placement directly.
---

# Simulate GFS Policy

Convert a finite action into executable GFS configuration, then preserve all evidence.

## Workflow

1. In AgentTeams, submit one cataloged action and window through the authenticated SchedNav MCP server:

```bash
mcporter call schednav.simulate_policy --args '{"idempotency_key":"<stable-task-key>","run_config_id":"stress-gpu-series-2-2024-04-12","action_id":"<catalog-action-id>"}'
```

2. Poll the returned opaque task ID with `schednav.get_task`. Do not claim completion until `status=succeeded`; preserve a returned `failed` state as evidence.
3. Inspect the returned materialization receipt; reject any action outside the declared controlled fields.
4. Use the returned TraceManifest, RunManifest, MetricsReport and applicable gate references. Read only schema-approved JSON with `schednav.read_artifact`; the bridge rejects raw CSV/log access. The host bridge performs validation, preparation, isolated execution and metrics extraction in one serialized task.
5. For deterministic attestation, submit another logical task and compare the two run manifests outside the policy-selection step.

## Guardrails

- Use GFS as the only placement executor.
- Never reuse mutable Trace/Cluster state across policies; every run needs a new process.
- Never overwrite an existing run directory.
- Keep the real Trace, upstream GFS, checkpoints, and artifacts outside published source.
- A successful process is not enough: require CSV hashes, canonical metrics, consistent preemption/run/guarantee ledgers, and applicable gates.
- Do not claim improvement until same-window policy comparison and SLO audit are complete.
- Never print, persist, or forward AgentTeams gateway credentials; MCP authorization is injected by the runtime.
