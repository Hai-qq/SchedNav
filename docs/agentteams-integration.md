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

## Model contract

Every role is locked to `deepseek-v4-flash`. The bundle builder rejects every other model ID and disables embedding. The model generates plans and high-level hypotheses only; deterministic Python code performs simulation, comparison, SLO audit and ranking.

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
- `get_task`;
- `read_artifact`.

It rejects unknown actions, unknown SLOs, unexpected arguments, unsafe paths, invalid artifact schemas and missing/invalid bearer identity. There is no shell, arbitrary Python or placement endpoint. Execution uses one host lane to avoid shared mutable simulator state.

Native run configs use:

```json
{
  "schema_version": "schednav.native-run-config/v1",
  "trace_manifest": "datasets/local/trace.json"
}
```

The path must remain inside the project root and identify a valid content-addressed `schednav.trace/v1` manifest. Policy files must use `schednav.simulation-policy/v1` and be listed in the bridge catalog.

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
