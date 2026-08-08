---
name: audit-gpu-slo
description: Audit a fingerprinted GFS MetricsReport against an explicit schednav.slo-spec/v1 using deterministic hard and soft constraints. Use for SLO compliance, candidate elimination, or evidence-backed policy review. Never invent business thresholds or let natural-language judgment override the audit result.
---

# Audit GPU SLO

Apply user- or business-provided constraints to canonical simulation evidence.

## Workflow

1. Require the final `configs/slos/schednav-demo-slo-v1.json` or another explicit `schednav.slo-spec/v1`; if thresholds are missing, stop and request them.
2. Confirm every metric name belongs to the canonical catalog.
3. For baseline-relative constraints, require canonical FIFO metrics from the same source, Trace window and population.
4. In AgentTeams, submit the audit through the authenticated SchedNav MCP server, using only artifact references under the configured artifact root:

```bash
mcporter call schednav.audit_slo --args '{"idempotency_key":"<stable-task-key>","metrics_ref":"<metrics-ref>","slo_spec_id":"schednav-demo-slo-v1","baseline_metrics_id":"stress-fifo-slo-v1-final"}'
```

5. Poll the returned opaque task ID with `schednav.get_task`; continue only after `status=succeeded`, then inspect the structured audit through `schednav.read_artifact`.
6. For trusted local development outside AgentTeams, the equivalent direct command is:

```powershell
$env:PYTHONPATH = "src"
.venv-gfs\Scripts\python.exe -m schednav.cli audit-slo `
  --metrics <metrics.json> --baseline <fifo-metrics.json> `
  --slo <slo.json> --output <audit.json>
```

7. Reject a policy when any hard constraint or required evidence check fails. Preserve soft violations for the Manager.
8. Cite the metrics, FIFO baseline, SLO and audit fingerprints in any recommendation.

## Guardrails

- Never create SLO thresholds from Trace statistics, model intuition, or desired outcomes.
- Treat unavailable metrics as failed/unavailable, not as zero.
- Hard constraints plus preemption, Spot run-start and Spot guarantee ledger consistency and FIFO baseline compatibility determine `audit_passed`; soft constraints remain explicit tradeoffs.
- Do not recommend a policy solely because it passes; compare all surviving candidates using simulation evidence.
- Never print, persist, or forward AgentTeams gateway credentials; MCP authorization is injected by the runtime.
