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

For a registered run set, call `audit_run_set` with the simulation index and FIFO action ID. Preserve every window's `selected`, `tie_requires_human_approval`, or `no_eligible_policy` state. Report the aggregate frontier and robustness counts, but never declare a cross-window universal winner.

For a chronological holdout controller, audit each proposed candidate against that window's FIFO baseline, preserve infeasible windows, and compute oracle coverage/regret only after the pre-simulation controller artifact is finalized.
