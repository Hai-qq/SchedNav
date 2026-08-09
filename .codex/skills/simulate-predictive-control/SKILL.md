---
name: simulate-predictive-control
description: Execute one cataloged scheduling policy under a cataloged past-only predictive Spot quota controller and preserve simulation, forecast, quota, feedback and metrics evidence. Use for shadow evaluation of a fixed policy/controller pair. Never inspect hidden future workload before a decision or choose placement in prose.
---

# Simulate Predictive Control

1. Require a cataloged run configuration, PolicyAction and registered aggregate or tenant predictive-controller profile. Reject a tenant profile paired with trace/v1, missing tenant IDs or wildcard resource pools.
2. Confirm the policy guarantee duration is covered by a declared controller guarantee horizon.
3. Call `simulate_predictive_policy` with `run_config_id`, `action_id` and `controller_id`.
4. Return the run-spec, simulation-result, metrics and predictive-control-report references.
5. Require the controller report to show a monotonic observation cutoff, deterministic forecast/model fingerprints, quota updates, post-observation forecast scoring and, for the tenant profile, per-resource-pool guarantee feedback totals.
6. Preserve failed or starved runs as evidence. Never replace them with an LLM estimate.

The simulator owns queue order, concrete placement and eviction victims. The controller may limit new Spot admissions but does not forcibly evict running Spot jobs when a quota falls. HP preemption remains a deterministic scheduler action. Tenant feedback records each completed guarantee period and job completion as success and preemption as failure, weighted by allocated node count; this feedback ledger does not replace the formal run-based SLO ledger.

The current operation replays one fixed policy/controller pair from the trace start and audits its declared evaluation window. It is not a stateful live-policy switch at the forecast cutoff; do not present it as one.
