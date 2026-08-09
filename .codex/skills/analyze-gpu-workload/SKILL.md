---
name: analyze-gpu-workload
description: Analyze a fingerprinted canonical GPU cluster Trace and produce a structured workload summary covering HP/Spot arrivals, GPU demand, duration, active pressure and regime signals. Use before policy selection on any supported dataset. Never infer missing service-class labels or recommend a policy without simulation evidence.
---

# Analyze GPU Workload

1. Validate the canonical Trace:

```powershell
schednav validate-trace --trace <trace.json>
```

2. Analyze the selected window:

```powershell
schednav analyze-trace `
  --trace <trace.json> `
  --sample-interval-seconds 3600 `
  --output <workload-summary.json>
```

3. Return the Trace fingerprint, source provenance, population, demand statistics, pressure signals and workload fingerprint.
4. Treat sampled active demand as trace-intended demand, not simulated allocation.
5. If the source does not publish HP/Spot classes, surface the adapter's explicit mapping and restrict unsupported SLO claims.

For a registered multi-window study, call the bridge's `analyze_run_set` with its `run_set_id`. Return the run-set fingerprint and the ordered per-window workload artifact references; do not merge unlike windows into one workload regime.

For an adaptive holdout study, verify `schednav.adaptive-study-design/v1`, its fingerprint and chronological split before summarizing calibration/evaluation regimes. Use only declared workload fields; do not inspect policy metrics or change the split after simulation.

Do not pass raw Trace files into the Agent context. Do not select or rank policies from workload statistics alone.

For a real-time or rolling decision, analyze only state ending at the declared cutoff, then invoke `$forecast-gpu-demand`. A complete future-window workload summary is forbidden as a controller input.
