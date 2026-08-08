---
name: select-bounded-gfs-policies
description: Select distinct executable SchedNav PolicyAction files from a finite versioned action space using structured workload evidence. Use when the Scheduling Strategist must form a counterfactual candidate set before GFS simulation. Never invent unlisted parameters, placements, performance claims, or a winner.
---

# Select Bounded GFS Policies

Choose policy candidates without escaping the validated control boundary.

## Workflow

1. Require a fingerprinted `schednav.workload-summary/v1` and a versioned action-space file. In AgentTeams, read the small workload document through `schednav.read_artifact`; never request raw Trace rows.
2. Require the action space status to permit evaluation and inspect its exact `profiles` catalog.
3. Select distinct existing `schednav.policy-action/v1` files that exactly match catalog profiles.
4. Explain each candidate only as a hypothesis tied to workload signals; do not predict its metrics.
5. Return action IDs and artifact paths. The Simulation Agent must materialize and run every selected candidate.

## Guardrails

- Never output Job, node, GPU, placement, arbitrary code, estimator architecture, or training hyperparameters.
- Never synthesize a cross-product from individually allowed values; an action must exactly match one curated profile.
- Never claim that a bounded or previously executable action is performant; require same-window GFS evidence.
- If fewer than three distinct actions exist, report the catalog gap instead of fabricating 3–5 candidates.
- Do not rank candidates before canonical metrics, policy comparison, and explicit SLO audit exist.
- Never print, persist, or forward AgentTeams gateway credentials; MCP authorization is injected by the runtime.
