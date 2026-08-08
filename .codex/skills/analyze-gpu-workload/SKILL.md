---
name: analyze-gpu-workload
description: Analyze a real Alibaba GPU trace window into a fingerprinted WorkloadSummary with HP/Spot arrivals, requested GPU-hours, burstiness, active-demand pressure, and source hashes. Use for workload analysis, regime evidence, GPU demand summaries, or before generating high-level SchedNav policies. Do not use it to infer simulated placement or performance.
---

# Analyze GPU Workload

Produce structured evidence before proposing policy changes.

## Workflow

1. Require a local Trace directory, GPU model, and explicit evaluation timestamps.
2. In an AgentTeams Worker, submit only a cataloged window through the authenticated SchedNav MCP server:

```bash
mcporter call schednav.analyze_workload --args '{"idempotency_key":"<stable-task-key>","run_config_id":"stress-gpu-series-2-2024-04-12"}'
```

3. Poll the returned opaque task ID with `schednav.get_task`; continue only after `status=succeeded`. Read the small structured result with `schednav.read_artifact` and pass its fingerprint/reference downstream rather than raw rows.
4. For trusted local development outside AgentTeams, the equivalent direct command is:

```powershell
$env:PYTHONPATH = "src"
.venv-gfs\Scripts\python.exe -m schednav.cli analyze-workload `
  --trace-dir <trace-dir> --gpu-model <model> `
  --evaluation-start "<timestamp>" --evaluation-end "<timestamp>" `
  --output <ignored-artifact-path>
```

5. Pass the resulting `schednav.workload-summary/v1` artifact or its fingerprint to downstream workers.

## Guardrails

- Treat sampled active demand as trace intent, not simulated allocation.
- Keep `gpu_request * worker_num` as the demand unit.
- Do not invent categorical pressure thresholds; use the reported regime signals.
- Do not copy raw Trace rows into Agent context or a public repository.
- Do not recommend a policy from workload evidence alone; require GFS simulation.
- Never print, persist, or forward AgentTeams gateway credentials; MCP authorization is injected by the runtime.
