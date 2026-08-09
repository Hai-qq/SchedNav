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
| Workload Analyst | `analyze-gpu-workload` | canonical Trace reference | WorkloadSummary reference |
| Scheduling Strategist | `select-bounded-policies` | workload + action space | 3–5 Policy references |
| Simulation Agent | `simulate-gpu-policy` | Trace + Policy | SimulationResult + MetricsReport references |
| SLO Auditor | `audit-gpu-slo` | metrics + SLO + FIFO baseline | SLOAudit reference |
| Manager | `compare-gpu-policies` | metrics + audits | Portfolio + Ranking + approval request |

For a registered multi-window batch, the same roles exchange `run_set_id` and compact artifact references: the analyst calls `analyze_run_set`, the simulator calls `simulate_run_set`, and the auditor calls `audit_run_set`. Every window remains an independent SLO and ranking decision.

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
- `simulate_policy`;
- `compare_policies`;
- `audit_slo`;
- `rank_policies`;
- `analyze_run_set`;
- `simulate_run_set`;
- `audit_run_set`;
- `get_task`;
- `read_artifact`.

It rejects unknown actions, unknown run sets, batches outside 3–5 actions or 1–2 repetitions, unknown SLOs, unexpected arguments, unsafe paths, invalid artifact schemas and missing/invalid bearer identity. There is no shell, arbitrary Python or placement endpoint. Execution uses one host lane to avoid shared mutable simulator state. Delegated identity validation uses a protected AgentTeams gateway route, bypasses ambient desktop proxies for the loopback check, and rejects explicit 401/403 responses.

Native run configs use:

```json
{
  "schema_version": "schednav.native-run-config/v1",
  "trace_manifest": "datasets/local/trace.json"
}
```

The path must remain inside the project root and identify a valid content-addressed `schednav.trace/v1` manifest. Containment checks use filesystem identity for existing Windows ancestors, so long paths and 8.3 aliases cannot create false escapes. Policy files must use `schednav.simulation-policy/v1` and be listed in the bridge catalog.

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

## Bundle build

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
python .\scripts\build_agentteams_bundle.py --project-root .
```

The builder reads `integrations/agentteams/package-manifest.json`, packages the five validated Skills and renders all role resources with `deepseek-v4-flash`. Generated bundles stay under ignored `dist/agentteams/`.

## Security verification

```powershell
.\scripts\start_host_bridge.ps1 -CheckOnly
.\scripts\test_host_bridge_safety.ps1
```

The safety test covers unlisted arguments, unknown actions/SLOs, path escape, idempotency conflict, missing/invalid authentication and schema-approved artifact reads.
