# Adaptive Holdout Evaluation

This study tests whether a workload-aware AgentTeams controller can retain the value of a five-action policy catalog while avoiding exhaustive evaluation on every holdout window. It is historical offline counterfactual evaluation, not online scheduling or forecasting.

## Frozen design

- Dataset: Alibaba `cluster-trace-v2026-spot-gpu`, pinned commit `0d0f3f1efdbf1add6a7bcc63676eafbd1eb11f71`.
- Cluster slice: `GPU-series-2`, 122 nodes and 976 GPUs.
- Eligible windows: all 112 complete origin-aligned days containing at least 20 HP and 20 Spot arrivals.
- Split: the first 67 eligible windows are calibration; the last 45 are chronological holdout evaluation.
- Carry-in: each day uses a fixed 30-day warm-up and drains selected jobs to completion.
- Repetitions: every one of the five actions runs twice from a fresh state, for 1,120 deterministic simulations.
- Freeze order: the design, workload-rule controller and AgentTeams controller were content-addressed before any v3 policy simulation.

The five-action v3 catalog contains FIFO, an unbudgeted preemptive reference with a 3,600-second Spot guarantee, and three 9%-eviction-budget actions. The guarded actions compare immediate versus 900-second HP preemption delay and longest-remaining versus lowest-checkpoint-loss victim rules. Agents choose only action IDs; the simulator still owns queue progression, concrete victims and placement.

The frozen design fingerprint is `e41ada5d5293e8630fa14387975924e5726ca2ff3ca8a0c7a6490689d2ce9672`. Its workload thresholds are calibration medians, not simulation-derived values: peak active pressure `0.909836` and Spot requested-GPU share `0.165147`.

## Controllers

The chronological holdout compares:

1. `fifo`: one fixed FIFO action;
2. `best_static`: one policy selected from calibration outcomes by hard-SLO pass count, allocation, Spot p95 JCT and eviction hierarchy;
3. `workload_rule`: a pre-registered workload-only rule selecting exactly three actions per holdout window;
4. `agentteams`: Workload Analyst plus Scheduling Strategist selecting three to five bounded actions per holdout window with `deepseek-v4-flash`;
5. `catalog_oracle`: all five actions, used only as an offline upper bound.

Every candidate is subsequently simulated and audited. A controller is feasible on a window when at least one of its candidates passes all eight hard SLOs. Formal oracle-frontier coverage means the controller's SLO-filtered, allocation-band, Spot-JCT and eviction frontier intersects the frontier obtained from all five actions. Ties remain ranges pending human approval. This hierarchy-aware measure is distinct from the secondary raw-allocation search diagnostic, and neither means the LLM predicted metrics.

## Results

Calibration selected `native-fifo` as the unique best static policy. On the 45 holdout windows, the catalog has at least one hard-SLO-compliant action in 41 windows.

| Controller | Candidate evaluations | Reduction vs all five | Feasible windows | Formal oracle-frontier coverage | Mean selected allocation uplift range vs FIFO | Conservative positive / equal / negative |
|---|---:|---:|---:|---:|---:|---:|
| FIFO / best static | 45 | 80.0% | 40/45 | 36/41 (87.8%) | 0.0000 pp | 0 / 40 / 0 |
| Workload rule | 135 | 40.0% | 41/45 | 39/41 (95.1%) | +0.0807 to +0.1051 pp | 4 / 37 / 0 |
| AgentTeams | 185 | 17.8% | 41/45 | 41/41 (100%) | +0.2092 to +0.2571 pp | 5 / 36 / 0 |
| Catalog oracle | 225 | 0.0% | 41/45 | 41/41 (100%) | +0.2092 to +0.2592 pp | 5 / 36 / 0 |

AgentTeams covers at least one action on the formal five-action oracle frontier in every feasible holdout window while evaluating 40 fewer policy-window candidates. Its complete selected-action set exactly matches the catalog frontier in 27/41 windows; the distinction is preserved because a local candidate set can retain additional hierarchy-equivalent ties. Its selected-frontier mean allocation uplift is a range because unresolved ties remain subject to human approval; even the conservative lower bound is positive in five windows, equal in 36 and negative in none. It also recovers one holdout window where FIFO fails a hard SLO but another catalog action passes.

The cost comparison matters. AgentTeams evaluates 50 more policy-window candidates than the three-candidate workload rule and therefore should not be described as a free improvement. It covers two additional formal oracle frontiers at that higher simulator budget. Conversely, it evaluates 40 fewer candidates than exhaustive search while retaining 100% formal frontier coverage. This is a bounded search-quality versus simulation-cost trade-off, not evidence that natural-language reasoning alone creates the performance gain.

For continuity with raw allocation search, the secondary diagnostic asks whether a candidate set contains the catalog's maximum-allocation hard-pass action before the one-percentage-point tie band and Spot metrics are applied. AgentTeams covers 39/41 such maxima with `0.0014` percentage points mean regret; the workload rule covers 33/41 with `0.1507` percentage points mean regret. These figures measure candidate discovery, not the final hierarchical recommendation, so they are not used as the headline outcome.

Across all 112 windows, the five actions produced 35 unique selections, 65 ties requiring human approval and 12 windows with no eligible policy. FIFO passes every hard SLO in 98/112 windows; the unbudgeted preemptive reference passes 60/112; the three guarded actions pass 80/112 each. Raw mean allocation is not used to override failed SLOs.

## AgentTeams execution

AgentTeams project `proj-20260809-080145` completed the pre-simulation phase with Workload Analyst and Scheduling Strategist, both locked to `deepseek-v4-flash`. The Manager validated exact 45-window coverage, 3–5 candidates per window, FIFO inclusion, allowed action IDs, design fingerprint and reason codes. The raw artifact SHA-256 is `ae9884f1429f7767cc3dff44d4da0d85bc9ef4b36c87d9718305d9ba487768b8`; the normalized controller fingerprint is `6e2056ea486bc697b9761a50346a31e545cad759f62d382ddd96b74fbb84f460`.

The project remains `approval_pending` by design. Simulation and SLO decisions are deterministic SchedNav evidence; AgentTeams does not approve a final deployment policy automatically.

## Reproduction

After producing a validated AgentTeams controller artifact, the complete deterministic pipeline is:

```powershell
.\scripts\run_adaptive_demo.ps1 `
  -DatasetDirectory C:\datasets\cluster-trace-v2026-spot-gpu `
  -AgentController C:\experiments\agentteams-controller.json `
  -OutputDirectory C:\experiments\schednav-adaptive-v3 `
  -Workers 8
```

The script freezes the design, evaluates all 112 windows, computes the chronological holdout benchmark and emits a compact public receipt. It refuses to overwrite an existing output directory. Raw traces, canonical per-job files, simulation results and AgentTeams logs remain outside Git.

Public evidence:

- [adaptive holdout receipt](../evidence/native-v3/alibaba-gpu-series-2-adaptive-holdout-v3.json);
- [all-112-window receipt](../evidence/native-v3/alibaba-gpu-series-2-all112-v3.json).

Key fingerprints:

- selection: `ab0914d803bff0fcc776bfe6142ac95c05bdc1e3adeb64a860398e89afde7e5c`;
- multi-window summary: `38bc7d74e65bf2d6b87af599bb6a8d60e52a1f337111d2bbe39612ad01a6f50e`;
- adaptive benchmark: `a6d448469dc2c7f8cf8c5070c6228b8817fdf8f9bf33a60e3fa2fdd51d70d8e5`;
- all-window receipt: `0bb852eb54621f43ba1ca7a597d1147f6947506b151bbc736106d4635127ce32`;
- adaptive receipt: `0cc4fba45838cf3b6448b27eb6924941cb9572358fbbb408ab9bd98186d2fc63`.

## Limitations

- AgentTeams sees the holdout windows' workload summaries. Future arrivals are therefore not hidden as they would be in rolling-horizon operation.
- The catalog oracle is an offline comparison bound, not a deployable scheduler.
- The comparison covers one GPU model slice, one SLO and five hand-designed actions.
- The workload rule uses fewer candidate evaluations than AgentTeams; their quality comparison must retain that cost difference.
- Four holdout windows have no eligible action. SchedNav preserves those failures instead of forcing a recommendation.
