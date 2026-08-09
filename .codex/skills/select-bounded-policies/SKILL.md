---
name: select-bounded-policies
description: Select three to five distinct executable GPU scheduling policies from a finite versioned SchedNav action space using structured workload evidence. Use when the Scheduling Strategist forms a counterfactual candidate set. Never invent unlisted parameters, placements, performance claims, weights, or a winner.
---

# Select Bounded Policies

1. Verify the workload summary schema and fingerprint.
2. Read the action space declared by the run configuration. The current adaptive study uses `configs/action_spaces/native-multiwindow-v3.json`; resolve only its listed policy files.
3. Select 3-5 distinct profiles that expose relevant trade-offs for the observed workload regime.
4. Return action IDs, policy paths, controlled fields, fixed execution controls and the selection rationale.
5. For a chronological holdout study, bind the candidate set to the frozen design fingerprint, cover every evaluation window exactly once and inspect no evaluation metrics before the artifact is finalized.
6. Mark each candidate `unverified` until same-trace simulation evidence exists.

Reject requests containing Job, Node, GPU IDs, placement, arbitrary code or unlisted policy cross-products. Workload evidence can justify candidate diversity but cannot establish policy quality.
