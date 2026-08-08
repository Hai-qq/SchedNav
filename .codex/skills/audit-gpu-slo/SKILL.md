---
name: audit-gpu-slo
description: Audit a fingerprinted canonical SchedNav MetricsReport against an explicit schednav.slo-spec/v1 using deterministic hard and soft constraints. Use for SLO compliance, candidate elimination or evidence-backed policy review. Never invent thresholds or let natural-language judgment override the audit result.
---

# Audit GPU SLO

1. Require a canonical metrics artifact with a valid fingerprint and consistent event ledgers.
2. Require an explicit versioned SLO spec. If it contains baseline-relative constraints, require same-source, same-trace, same-window FIFO metrics.
3. Run:

```powershell
schednav audit-slo `
  --metrics <metrics.json> `
  --slo <slo.json> `
  --baseline <fifo-metrics.json> `
  --output <audit.json>
```

4. Reject a candidate when any hard constraint or required evidence check fails.
5. Preserve soft failures for the declared hierarchical ranking stage.
6. Return observed values, resolved thresholds, pass/fail states and audit fingerprint.

Do not audit unavailable HP/Spot metrics as passed. Do not create a weighted LLM score or an undeclared tie-breaker.
