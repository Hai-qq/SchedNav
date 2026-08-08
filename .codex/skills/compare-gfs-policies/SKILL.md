---
name: compare-gfs-policies
description: Compare two or a portfolio of three to five canonical GFS MetricsReports from the same real Trace window and population, with source, fingerprint, completion, event-ledger, and control-parameter checks. Use when evaluating bounded SchedNav policies. Produces neutral deltas and never selects a winner by itself.
---

# Compare GFS Policies

Compare only attested, genuinely comparable evidence.

## Workflow

1. Require canonical `schednav.metrics-report/v1` files produced by isolated runs.
2. In AgentTeams, compare a three-to-five-policy portfolio through the authenticated SchedNav MCP server:

```bash
mcporter call schednav.compare_policies --args '{"idempotency_key":"<stable-task-key>","metrics_refs":["<metrics-a>","<metrics-b>","<metrics-c>"]}'
```

3. Poll the returned opaque task ID with `schednav.get_task`; read the resulting portfolio through `schednav.read_artifact`, then require `status=succeeded` and `comparable=true` before interpreting deltas.
4. For a trusted two-policy local diagnostic outside AgentTeams, run:

```powershell
$env:PYTHONPATH = "src"
.venv-gfs\Scripts\python.exe -m schednav.cli compare-policies `
  --left <metrics-a.json> --right <metrics-b.json> `
  --output <comparison.json>
```

5. For a trusted three-to-five-policy local diagnostic outside AgentTeams, run:

```powershell
$env:PYTHONPATH = "src"
.venv-gfs\Scripts\python.exe -m schednav.cli compare-portfolio `
  --metrics <metrics-a.json> <metrics-b.json> <metrics-c.json> `
  --output <portfolio.json>
```

6. Pass every candidate to the SLO auditor.
7. After audits, submit the declared hierarchy through MCP:

```bash
mcporter call schednav.rank_policies --args '{"idempotency_key":"<stable-task-key>","metrics_refs":["<metrics...>"],"audit_refs":["<audits...>"],"slo_spec_id":"schednav-demo-slo-v1"}'
```

For trusted local development, the equivalent command is:

```powershell
$env:PYTHONPATH = "src"
.venv-gfs\Scripts\python.exe -m schednav.cli rank-policies `
  --metrics <metrics...> --audits <audits...> --slo <slo.json> `
  --output <ranking.json>
```

8. Preserve an unresolved tie for human approval; do not add an unlisted tie-breaker.

## Guardrails

- Require the same GFS commit/patch, Trace commit/ID, evaluation window, HP/Spot population, and fixed execution controls.
- Require distinct admitted policy actions; the scheduler itself may remain the same while another bounded action field changes.
- Require completed populations and consistent preemption, Spot run-start and Spot guarantee ledgers.
- Read every delta as `right - left`.
- Report tradeoffs without hiding regressions or dividing by a zero baseline.
- Do not invent significance, causal claims, weighted aggregate scores, or an unlisted winner.
- Never print, persist, or forward AgentTeams gateway credentials; MCP authorization is injected by the runtime.
