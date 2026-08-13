# AgentTeams Integration

SchedNav uses AgentTeams as the multi-agent coordination runtime. It does not implement a parallel agent framework.

## Topology

```text
Manager
  ├─ Workload Analyst
  ├─ Scheduling Strategist
  ├─ Simulation Agent
  └─ SLO Auditor
```

The Manager owns task decomposition, status, artifact references, declared ranking and approval gates. Domain work is delegated to standalone Workers.

| Agent | Skill | Input | Output |
|---|---|---|---|
| Workload Analyst | `analyze-gpu-workload`, `forecast-gpu-demand` | canonical Trace reference; in rolling mode, cutoff + controller ID | WorkloadSummary or PredictiveObservationBundle reference |
| Scheduling Strategist | `select-bounded-policies` | workload/predictive bundle + action space | 3–5 Policy references |
| Simulation Agent | `simulate-gpu-policy`, `simulate-predictive-control` | Trace + Policy; in rolling mode, registered controller | SimulationResult + MetricsReport + PredictiveControlReport references |
| SLO Auditor | `audit-gpu-slo` | metrics + SLO + FIFO baseline | SLOAudit reference |
| Manager | `compare-gpu-policies` | metrics + audits | Portfolio + Ranking + approval request |

For a registered multi-window batch, the same roles exchange `run_set_id` and compact artifact references: the analyst calls `analyze_run_set`, the simulator calls `simulate_run_set`, and the auditor calls `audit_run_set`. Every window remains an independent SLO and ranking decision.

For the adaptive v3 holdout, candidate selection is a separate pre-simulation project. Workload Analyst verifies a frozen `schednav.adaptive-study-design/v1`; Scheduling Strategist emits `schednav.controller-selections/v1` covering each declared holdout window with 3–5 catalog actions and FIFO. Only after Manager validation does the deterministic experiment run. This ordering prevents evaluation metrics from leaking into Agent candidate generation.

For fixed-policy predictive control, the Manager declares a cutoff and cataloged controller. Workload Analyst calls `forecast_demand`, then passes only the resulting `schednav.predictive-observation-bundle/v1` reference to Scheduling Strategist. The bundle excludes later arrivals and the full trace fingerprint. Simulation Agent calls `simulate_predictive_policy`; SLO Auditor sees completed metrics only after the replay finishes. `configs/agentteams/host-bridge-predictive-v1.json` registers both the dependency-free aggregate controller and `tenant-predictive-spot-v1`, plus a `tenant-predictive-local` trace/v2 run. Its operation allowlist does not expose `analyze_workload`, static `simulate_policy` or run-set operations to that project. The host Python environment must install `.[forecast]` before selecting the tenant controller. The separate rolling path now adds common cutoff snapshots, past-only candidate scenarios and deterministic same-session state handoff; it remains historical shadow replay rather than a live cluster switch.

## Model contract

Every role is locked to `deepseek-v4-flash`. The bundle builder rejects every other model ID and disables embedding. The model generates plans and high-level hypotheses only; deterministic Python code performs simulation, comparison, SLO audit and ranking.

The integration contract is developed and tested against an AgentTeams `v1.2.1` source checkout. Source revisions and deployable images are separate facts: an installation must record the image reference or digest it actually resolves and must not infer that a matching image tag exists. AgentTeams source and runtime images are not vendored into this repository.

## Structured context

Agents exchange:

- artifact reference;
- schema version;
- SHA-256 fingerprint;
- task state;
- bounded summary.

Raw Trace files, per-job results, checkpoints and large logs are never inserted into LLM context or committed to the repository.

## Bounded host bridge

The MCP host bridge exposes only:

- `analyze_workload`;
- `forecast_demand`;
- `simulate_policy`;
- `simulate_predictive_policy`;
- `compare_policies`;
- `audit_slo`;
- `rank_policies`;
- `analyze_run_set`;
- `simulate_run_set`;
- `audit_run_set`;
- `get_task`;
- `read_artifact`.

It rejects unknown actions, unknown run sets, batches outside 3–5 actions or 1–2 repetitions, unknown SLOs, unexpected arguments, unsafe paths, invalid artifact schemas and missing/invalid bearer identity. There is no shell, arbitrary Python or placement endpoint. Execution uses one host lane to avoid shared mutable simulator state. Delegated identity validation uses a protected AgentTeams gateway route, bypasses ambient desktop proxies for the loopback check, and rejects explicit 401/403 responses.

The default bridge exposes every registered deterministic operation. A config may narrow task submission and MCP `tools/list` through `operation_allowlist`; this is the required profile for rolling predictive projects. `get_task` and schema-approved `read_artifact` remain read-only control-plane operations.

Native run configs use:

```json
{
  "schema_version": "schednav.native-run-config/v1",
  "trace_manifest": "datasets/local/trace.json"
}
```

The path must remain inside the project root and identify a valid content-addressed `schednav.trace/v1` or `schednav.trace/v2` manifest. The tenant controller additionally requires non-empty tenant IDs and concrete resource pools. Containment checks use filesystem identity for existing Windows ancestors, so long paths and 8.3 aliases cannot create false escapes. Policy files must use `schednav.simulation-policy/v1` and be listed in the bridge catalog.

A completed local multi-window experiment can be staged as one registered run set without publishing its per-job data:

```powershell
.\.venv\Scripts\python.exe .\scripts\prepare_agentteams_run_set.py `
  --experiment-directory C:\experiments\schednav-multiwindow-v2 `
  --run-set-id alibaba-v2-12d `
  --output-config artifacts\agentteams-multiwindow-v2\bridge-config.json

.\scripts\start_host_bridge.ps1 `
  -Config artifacts\agentteams-multiwindow-v2\bridge-config.json
```

The staging command validates the experiment/action-space fingerprint, copies only canonical run inputs into ignored local storage, registers at most 12 windows, and refuses to overwrite an existing run set or bridge config.

## Task state

Every submitted operation has an idempotency key and transitions through:

```text
queued → running → succeeded | failed
```

The bridge writes the normalized request, request fingerprint, idempotency receipt, task state and output references atomically. A reused key with different content fails closed.

## Human approval

Approval is required before:

1. an unbudgeted large simulation batch;
2. accepting a final recommendation;
3. adding an undeclared ranking rule to break a tie.

If declared metrics remain tied, the Manager returns `tie_requires_human_approval`. Human selection is recorded as an operational choice and is not rewritten as evidence that one tied policy is faster or better.

## Verified native-local workflow

Project `proj-20260808-104144` exercised the complete topology with every role on `deepseek-v4-flash`:

1. Workload Analyst called `analyze_workload` and produced workload fingerprint `49f0638a1278d85b1a4e4c045074d00e131ceb42200de2a1a0825c20310e2d33`.
2. Scheduling Strategist selected exactly the four registered native actions and emitted no placement fields.
3. Simulation Agent called `simulate_policy` four times with distinct idempotency keys and fresh bridge tasks.
4. SLO Auditor called `audit_slo` four times using the fresh `native-fifo` metrics artifact as the baseline.
5. Manager called `compare_policies` and `rank_policies`; the resulting portfolio fingerprint is `3a6bad71c54d012944434e475f0c426e2fb9e220bb7aca095f1698507ee26cdb` and ranking fingerprint is `5a8d161859458fd7c628207ecd06910e4ebaab875aed4e71e379f2457bdfb4b4`.

All delegated tasks reached `completed`; the project reached `completed` with outcome `approval_pending`. The ranking returned `tie_requires_human_approval` for the three preemptive actions. AgentTeams task state, full artifacts and logs remain local runtime evidence; the public repository contains only the aggregate [policy-evaluation receipt](../evidence/native-v1/alibaba-gpu-series-2-2024-04-12-policy-evaluation.json).

## Verified multi-window run-set workflow

Project `proj-20260809-065201` exercised the registered `alibaba-v2-12d` run set with every role on `deepseek-v4-flash` and finished as `completed / approval_pending`:

1. Workload Analyst ran `analyze_run_set` as task `task-20260809-070000`; bridge task `7ac4c8cac3d74f8086c88c26824c7b57` produced selection fingerprint `4178a4375addeee2856c6bc327526196a8d181a72d98d45c864bf64ca9eca3e5` and 12 workload references.
2. Scheduling Strategist completed `task-20260809-070100`, preserving exactly five registered actions. Its descriptive 9% budget typo was corrected to `spot_eviction_budget_rate=0.09`; the registered action IDs and configurations never changed.
3. Simulation Agent completed the formal `task-20260809-070200` with a new idempotency key after the strategist stage. Bridge task `6bea73dd35544cf6abaa16fd5c5a902f` produced 120 deterministic executions and run-set simulations fingerprint `51261ebc2177ba6bf50a96a8262f59c5750b94f43578a6e097d2ab82c27a9b98`. An earlier out-of-order batch remains local as discarded audit evidence and was not consumed downstream.
4. SLO Auditor ran `audit_run_set` as task `task-20260809-070300` against the formal simulation reference and `native-fifo` baseline. Bridge task `63353e5b58c44fdfaea9daf5f0a49825` produced audit fingerprint `e5bb6a2c344a0485428e853daf853ad83f5f120065fe5bfcc89ecedd64b7ed55` and multi-window fingerprint `38775d117d3434358b29819eb2ce5fe55eaaa0224bbf42a15acb4f0e91fe7ab5`.
5. Manager preserved the tool outcomes: 2 unique FIFO selections, 9 ties requiring human approval, 1 no-eligible window, and an eligible frontier above/equal/below FIFO in 7/4/0 feasible windows. The final proposal is retained in the local AgentTeams artifact store; no policy was auto-approved.

The AgentTeams summary matches the checked-in v2 receipt's metrics and decision counts. Its run-set fingerprints are execution-record fingerprints and therefore differ from the separately published experiment receipt fingerprints.

## Verified adaptive holdout selection workflow

Project `proj-20260809-080145` completed the pre-simulation controller stage as `completed / approval_pending` with Workload Analyst and Scheduling Strategist on `deepseek-v4-flash`:

1. SchedNav froze design fingerprint `e41ada5d5293e8630fa14387975924e5726ca2ff3ca8a0c7a6490689d2ce9672` before v3 simulation. It declares all 112 eligible windows, the chronological 67/45 split, the five-action catalog and workload-only thresholds.
2. Workload Analyst completed `task-20260809-080200`, independently verifying the fingerprints, split, action IDs and calibration medians without calling simulation, audit, comparison or ranking tools.
3. Scheduling Strategist completed `task-20260809-080300` and produced 45 exact window entries: 18 contain three candidates, four contain four and 23 contain all five. Every entry includes FIFO and only declared action IDs.
4. Manager validated exact coverage and constraints, then published raw artifact `shared/projects/proj-20260809-080145/artifacts/agent-controller-raw.json` with SHA-256 `ae9884f1429f7767cc3dff44d4da0d85bc9ef4b36c87d9718305d9ba487768b8`.
5. SchedNav normalized that artifact to controller fingerprint `6e2056ea486bc697b9761a50346a31e545cad759f62d382ddd96b74fbb84f460` before starting 1,120 deterministic simulations.

On the 45-window holdout, the AgentTeams candidate sets cover at least one action on 41/41 feasible formal catalog frontiers using 185 candidate evaluations, compared with 39/41 using 135 evaluations for the fixed three-candidate workload rule and 41/41 using 225 evaluations for exhaustive catalog search. Exact frontier-set matches and raw maximum-allocation candidate coverage are reported separately. The latter precedes the formal allocation-band, Spot-JCT and eviction hierarchy. The comparison therefore reports both decision quality and simulation cost. Full evidence is in [Adaptive Holdout Evaluation](adaptive-holdout-evaluation.md); raw AgentTeams rooms, task workspaces and logs remain local.

## Verified predictive shadow workflow

Project `proj-20260809-110524` exercised the predictive-only bridge with every role on `deepseek-v4-flash` and finished as `completed / no_eligible_policy`:

1. Workload Analyst completed `task-20260809-110601`. Bridge task `5670792d293f4ae2a2b956e340b23c5b` generated the cutoff observation at `3628800`; its bundle fingerprint is `29122b91e1e26e9ce32ba7e61f8d346e9655d954183aeed268c44e8bd9568efd`. The artifact records that later arrivals were excluded, future demand was not used and the maximum observed time equals the cutoff.
2. Scheduling Strategist completed `task-20260809-110602`, preserving exactly `native-preemptive-g3600-b09-d0000`, `native-preemptive-g3600-b09-d0900` and `native-preemptive-g3600-b09-loss-aware` as shadow hypotheses. It did not run simulation or inspect realized metrics.
3. Simulation Agent completed `task-20260809-110603`. Predictive bridge tasks `bd2c5294964543749f081e1acb24d85c`, `6bb63c259d7c457cb010b3017b54cd3d` and `0c5c0054ef504947b5ad66c9416dbe81` each ran once and produced metrics fingerprints `308d3cf9b208376e1038714e3a3b17cd1c3c32c6c081c11e9bc0bd6b50c26438`, `af70e3b0056655ce845e234b46e30a5ae84f2e369f3b875dc096da6d09f32e5d` and `f6a81d967290eedb5b338775b1ee003a2b6385aa1c5bcfb41c18b73ac45d833d`. All three reports measured P90 coverage of `0.941860465`.
4. SLO Auditor completed `task-20260809-110604` using the ordinary FIFO artifact with metrics fingerprint `1d86d5adba05e269e775c943fc87322867ee7ebb879ae9ba66a00ef89f174d97`, not a quota-limited predictive FIFO. Every candidate passed seven of eight hard constraints and failed only `allocation-fifo-nondegradation`: allocation rates `0.735174`, `0.737623` and `0.736233` were below the FIFO value `0.790511`.
5. Manager called `compare_policies` and `rank_policies`. Portfolio fingerprint `da1e12551f480985c2a45c85584c0c8a92e6a1e1b97ffcde6db4644f664e0779` and ranking fingerprint `87787bfc719f859ce1f7393ca23fe94e59feb536529d26afdd15ce6076027b7e` preserve the result: `selection_status=no_eligible_policy` and an empty selected-policy set.

This run verifies the real Manager/Worker handoff, cutoff-safe artifact boundary, deterministic replay, audit and hard-SLO-first rejection path. It does not show performance superiority, a production scheduler adapter or a live policy switch at the cutoff. Full task workspaces, rooms and large replay artifacts remain local runtime evidence and are not committed to the public repository.

That project used the lightweight aggregate controller. The subsequent tenant-aware increment keeps the same AgentTeams roles, Skills and bridge operations; no new agent framework or free-form model action was introduced. Bounded bridge tasks `f994e65b1db6463b9de801f2e3889fde` (`forecast_demand`) and `dca77dbd7aaf4882b951292487f89b79` (`simulate_predictive_policy`) both succeeded with `tenant-predictive-local + tenant-predictive-spot-v1`; their forecast and metrics fingerprints exactly match the direct deterministic runs. The checked-in [tenant-predictive receipt](../evidence/predictive-v1/alibaba-gpu-series-2-2024-04-12-tenant-predictive.json) records this bridge proof together with two deterministic forecasts and two deterministic closed-loop replays. It passes seven of eight hard SLOs and is rejected for allocation non-degradation. This is execution evidence for the new deterministic worker tool path, not a claim that a second live AgentTeams room has already produced a better policy.

The subsequent 11-window predictive calibration/holdout study was executed by the deterministic runner, not by an Agent. It evaluates FIFO, guarded-static, aggregate-predictive and tenant-predictive arms twice per window and writes a selection lock before holdout. Calibration produced `no_eligible_arm`; holdout then recorded 5/5 hard-SLO passes for FIFO and guarded-static, 1/5 for tenant-predictive and 0/5 for aggregate-predictive. The [public receipt](../evidence/predictive-v2/alibaba-gpu-series-2-predictive-multiwindow-v1.json) therefore constrains AgentTeams behavior: tenant prediction remains a shadow hypothesis, the Auditor must reject its allocation regressions, and Manager cannot claim that multi-agent orchestration has improved scheduling performance. The completed outer rolling study follows that rule with past-only state and exact handoff, but also fails to beat FIFO; its evidence is documented below.

## Verified predictive multi-window evidence gate

Project `proj-20260809-160234` separately exercised the complete Manager/Worker topology as a read-only gate over the already frozen study. Every role used `deepseek-v4-flash`; no role called simulation, audit, comparison or ranking tools, and no new performance value was generated:

1. Workload Analyst completed `task-20260809-160300` with 44/44 checks over the 11-window set, chronological 6/5 split, cutoffs and no-future boundary.
2. Scheduling Strategist completed `task-20260809-160400` with 54/54 checks over the four frozen arms, pre-holdout selection lock, absence of post-hoc candidates and absence of a weighted score.
3. Simulation Agent completed `task-20260809-160500` with 39/39 primary and 9/9 fingerprint-pattern checks over 88 receipts, 88 metrics artifacts and 44 deterministic two-repetition cells.
4. SLO Auditor completed `task-20260809-160600` with 30/30 checks, independently reproducing the hard-SLO pass counts and confirming that thresholds were unchanged and no winner was stated.
5. Manager retained the human gate as `approval_pending` and wrote the only evidence-supported decision: `no_calibration_eligible_arm`, `selected=[]`; holdout results remain diagnostic and cannot modify the frozen selection.

The gate consumed a 249-file local evidence set whose manifest covered every file except the manifest itself. Raw rows, per-job results, task workspaces, rooms and the Manager decision record remain local runtime evidence; the repository publishes only the compact receipt and these reproducibility facts.

## Verified rolling decision workflow

Project `proj-20260810-062224` completed the full outer rolling loop with every
LLM stage fixed to `deepseek-v4-flash`:

1. Five `single_agent` and five `multi_agent` controllers each advanced through
   six four-hour cutoffs. Every decision task was bound to one cutoff observation
   and dispatched after a private-room context clear.
2. Workload Analyst produced 30 structured multi-Agent analyses. Scheduling
   Strategist produced 30 single-Agent and 30 multi-Agent candidate decisions.
   Every candidate set contained exactly three registered actions including
   FIFO; the collector recomputed all 90 normalized-stage byte hashes.
3. Simulation Agent advanced the ten frozen decision prefixes in six waves,
   preserved simulator and predictor state, and revealed actual future arrivals
   only after each action was fixed. The resulting five-window study ran every
   complete arm/window twice and recorded identical repetition fingerprints.
4. SLO Auditor task `task-20260811-071001` independently verified 30/30 record
   fingerprints, 30/30 deterministic repetition pairs and 15/15 deployable
   rolling boundary/state-handoff records. It reproduced hard-SLO pass counts
   of 5/5 for ordinary FIFO and 1/5 for the fixed predictor, workload rule,
   single-Agent and multi-Agent arms.
5. Manager task `task-20260811-071100` consumed the Auditor's byte-hashed output,
   excluded the future-aware oracle and applied the declared hierarchy. Only
   `ordinary-fifo` passed every evaluated window, so it is the bounded fallback
   recommendation. Both Agent-superiority gates remain `not_established`.

The project is `completed / approval_pending`; no production change was applied.
The Agent layer has therefore demonstrated structured delegation, context
isolation, bounded action enforcement, deterministic handoff, audit and safe
fallback—not better scheduling performance. The public
[rolling ablation receipt](../evidence/rolling-v1/alibaba-gpu-series-2-rolling-ablation-v1.json)
and [AgentTeams closeout receipt](../evidence/rolling-v1/alibaba-gpu-series-2-rolling-agentteams-closeout-v1.json)
bind the aggregate evidence, Auditor and Manager outputs without publishing raw
Trace rows or model conversations.

## Verified rolling v2 decision workflow

Project `proj-20260811-042605` repeated the full workflow on the separately
frozen 2024-08-21 through 2024-08-25 holdout with the v2 past-shaped candidate
evaluator:

1. Five single-Agent and five multi-Agent controllers each completed six
   cutoff-safe decisions. The exported plan manifest binds 90 normalized
   stages—30 single-Agent Strategist stages and 30 Analyst + 30 Strategist
   multi-Agent stages—to fingerprint
   `5a32176b7c4234b1462644e7536fb59df615677c6062dd03c939595e346320ac`.
2. The final-wave generic runner completed all ten bridge calls but could not
   satisfy its obsolete requirement for a next-window checkpoint after the
   sixth and final decision. Terminal recovery task `task-20260811-212000`
   reused those exact ten successful results, made zero new advance calls,
   read no large simulation result into Agent context, and produced ten
   terminal receipts plus summary fingerprint
   `063d630828fc5cd28acd629b1e9ebd99bcd65459f14f6483a7540ad2700ed934`.
   No wave-07 checkpoint was fabricated.
3. The deterministic experiment produced 30 arm/window records with two
   identical repetitions each. FIFO, workload rule, single Agent and multi
   Agent each passed 4/5 windows; fixed prediction passed 2/5. Their detailed
   aggregate values are bound by the public v2 ablation receipt.
4. SLO Auditor task `task-20260811-214000` verified all 30 record fingerprints,
   all 30 repetition pairs and all 15 deployable rolling boundaries. Manager
   task `task-20260811-214100` consumed its byte receipt, excluded the
   future-aware oracle and found no deployable arm that passed 5/5 windows.

The final decision is `eligible=[]`, `recommended=null`, both superiority gates
are `not_established`, and the project is `completed / approval_pending` with no
production change. The public [v2 ablation receipt](../evidence/rolling-v2/alibaba-gpu-series-2-rolling-ablation-v2.json)
and [v2 AgentTeams closeout receipt](../evidence/rolling-v2/alibaba-gpu-series-2-rolling-agentteams-closeout-v2.json)
bind this result without publishing Trace rows, task workspaces or model
conversations.

## Verified rolling v3 matched-handoff workflow

Project `proj-20260812-190350` completed the matched Analyst-handoff ablation on
the separately frozen 2024-08-26 through 2024-08-30 holdout:

1. Five single-Agent, five full multi-Agent and five masked multi-Agent
   controllers each completed 12 two-hour decisions with exact state handoff.
   The plan collector verified 300 normalized `deepseek-v4-flash` stages and
   manifest fingerprint
   `5043abc0e5f5dffafe24af05f1dc0a9d26236c3b39b1b7d69efdac734453c53a`.
2. Full and masked multi-Agent arms used identical Strategist instructions,
   five windows, 120 accepted model calls and 360 candidate simulations. The
   only intended handoff difference was structured Analyst content versus the
   fixed mask. Every accepted task was bound to one observation; wave 05 onward
   used a fresh one-use private room because delayed completion messages made
   reusable-room clears insufficient.
3. The deterministic hidden-future execution produced 35 records with two
   identical repetitions each. FIFO passed 5/5 windows, workload rule and
   masked multi-Agent 4/5, single-Agent and full multi-Agent 3/5, and fixed
   prediction 2/5. Full multi-Agent therefore tied single-Agent and ranked
   worse than its matched masked control.
4. SLO Auditor task `task-v3-20260813-closeout-audit` verified 35/35 record
   fingerprints, 35/35 repetition pairs, 20/20 rolling arm/window chains and
   all aggregate/causal fields. Manager task
   `task-v3-20260813-closeout-manager` consumed its byte receipt, excluded the
   future-aware oracle and returned
   `eligible=[ordinary-fifo] / recommended=ordinary-fifo`.

The project is `completed / approval_pending`; no production change was
applied. `multi_agent_superiority_gate`, `multi_agent_vs_ordinary_gate` and
`analyst_causal_value_gate` are all `not_established`. The result establishes
the executable multi-role evidence and safety-control path, but it does not
establish scheduling-performance value from adding Agents. Because non-Agent
holdout outcomes were visible before the matched-prompt amendment, the Analyst
comparison is explicitly exploratory rather than fully blinded. The public
[v3 ablation receipt](../evidence/rolling-v3/alibaba-gpu-series-2-rolling-ablation-v3.json)
and [v3 AgentTeams closeout receipt](../evidence/rolling-v3/alibaba-gpu-series-2-rolling-agentteams-closeout-v3.json)
bind the aggregate evidence, Auditor and Manager outputs.

## Bundle build

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
python .\scripts\build_agentteams_bundle.py --project-root .
```

The builder reads `integrations/agentteams/package-manifest.json`, packages the seven validated Skills and renders all role resources with `deepseek-v4-flash`. Generated bundles stay under ignored `dist/agentteams/`.

## Security verification

```powershell
.\scripts\start_host_bridge.ps1 -CheckOnly
.\scripts\test_host_bridge_safety.ps1
```

The safety test covers unlisted arguments, unknown actions/SLOs, path escape, idempotency conflict, missing/invalid authentication and schema-approved artifact reads.
