---
name: select-bounded-policies
description: Select three to five distinct executable GPU scheduling policies from a finite versioned SchedNav action space using structured workload evidence. Use when the Scheduling Strategist forms a counterfactual candidate set. Never invent unlisted parameters, placements, performance claims, weights, or a winner.
---

# Select Bounded Policies

1. Verify the workload summary schema and fingerprint.
2. Read `configs/action_spaces/native-v1.json` and resolve only its listed policy files.
3. Select 3-5 distinct profiles that expose relevant trade-offs for the observed workload regime.
4. Return action IDs, policy paths, controlled fields, fixed execution controls and the selection rationale.
5. Mark each candidate `unverified` until same-trace simulation evidence exists.

Reject requests containing Job, Node, GPU IDs, placement, arbitrary code or unlisted policy cross-products. Workload evidence can justify candidate diversity but cannot establish policy quality.
