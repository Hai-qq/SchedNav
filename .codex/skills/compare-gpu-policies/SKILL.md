---
name: compare-gpu-policies
description: Compare a portfolio of three to five canonical SchedNav MetricsReports from the same Trace fingerprint, evaluation window and population, with source, fingerprint, completion, ledger and execution-control checks. Use for neutral counterfactual policy comparison. Never select a winner without SLO audits and the declared ranking hierarchy.
---

# Compare GPU Policies

1. Require 3-5 `schednav.metrics-report/v2` artifacts.
2. Run the portfolio comparison:

```powershell
schednav compare-portfolio `
  --metrics <metrics-a> <metrics-b> <metrics-c> `
  --output <portfolio.json>
```

3. Require matching source, Trace fingerprint/ID, evaluation window, population and fixed execution controls.
4. Require valid fingerprints, complete populations and consistent preemption/run/guarantee ledgers.
5. Report right-minus-left metric deltas without causal or winner claims.
6. Pass metrics to `$audit-gpu-slo`, then apply `schednav rank-policies` only with matching audits and the declared SLO.

Do not compare absolute metrics across unrelated datasets or windows.
