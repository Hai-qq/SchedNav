---
name: simulate-gpu-policy
description: Execute one cataloged high-level GPU scheduling policy in the first-party deterministic SchedNav simulator and preserve canonical result, metrics, run, event-ledger, and fingerprint evidence. Use for counterfactual policy evaluation or deterministic replication. Never choose Job-to-Node/GPU placement directly or simulate outcomes in prose.
---

# Simulate GPU Policy

1. Require a validated `schednav.trace/v1` manifest and `schednav.simulation-policy/v1` file.
2. Confirm the policy comes from the declared finite action space and contains no placement identifiers.
3. Run one policy from a fresh trace state:

```powershell
schednav simulate `
  --trace <trace.json> `
  --policy <policy.json> `
  --result <simulation-result.json> `
  --metrics <metrics.json>
```

4. Return artifact references plus trace, policy, result and metrics fingerprints.
5. Preserve failed runs as failure evidence; never replace a failed simulation with an estimate.

Keep raw Trace files and per-job results outside published source. The engine, not the Agent, owns queue ordering, preemption and deterministic placement.

For an explicitly approved registered multi-window study, call `simulate_run_set` with the `run_set_id`, three to five registered `action_ids`, and one or two repetitions. Require identical result and metrics fingerprints across repetitions. Return the run-set simulation index reference; never paste per-job results into Agent context.
