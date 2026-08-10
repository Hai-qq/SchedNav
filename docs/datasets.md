# Dataset Support

SchedNav is dataset-neutral after ingestion. Every source is normalized into the canonical trace contract documented in [trace-contract.md](trace-contract.md).

| Dataset | Adapter | Canonical contract | Published labels | Redistribution |
|---|---|---|---|---|
| Alibaba `cluster-trace-v2026-spot-gpu` | `schednav import-alibaba` | trace/v2; `organization → tenant_id` | HP / Spot | Raw and per-job converted data are not committed. |
| Alibaba `cluster-trace-gpu-v2023` | `schednav import-alibaba-v2023` | trace/v1 | LS / BE, mapped explicitly to HP / Spot | Raw and per-job converted data are not committed. |
| Microsoft Philly GPU Trace | `schednav import-philly` | trace/v1 | No HP / Spot label | CC BY 4.0 source is downloaded separately; raw and converted data are not committed. |
| Private or additional public traces | Canonical adapter contract | trace/v1 or source-backed v2 | Adapter-defined and provenance-recorded | Controlled by the source license and data policy. |

## Philly acquisition

- Official source: <https://github.com/msr-fiddle/philly-traces>
- Pinned commit: `29a1b87fa2d9ed80b83c9e3a37f3a88d382b031d`
- Dataset license: CC BY 4.0

After extracting `cluster_job_log` and `cluster_machine_list` outside this repository:

```powershell
schednav import-philly `
  --job-log C:\datasets\philly\cluster_job_log `
  --machine-list C:\datasets\philly\cluster_machine_list `
  --output-dir C:\datasets\philly\canonical-hp `
  --service-class HP
```

The explicit `HP` mapping means this converted dataset can validate ingestion, workload statistics, placement, completion and utilization. It cannot honestly validate Spot eviction or HP-vs-Spot trade-offs. Those metrics remain unavailable until a defensible class mapping is supplied.

## Verified Philly run

The official archive (`1,055,988,361` bytes, SHA-256 `2037ccf63a725a02be718b0e1aeee6142c90b7f62d0d849295120e9f6ba13d2c`) produced 111,846 valid jobs and skipped 5,479 records without a usable completed attempt. An origin-preserving 1,000-job prefix was simulated twice with `native-fifo`; both runs produced result fingerprint `b94366a6e396beeab83313cdc33141818f56e1804fc4c75e5ee8f1ea94b98d73` and metrics fingerprint `f858e144b293cd6b5982c73c37a300fe5568f62c916a568a7d984e04d029687c`.

The public aggregate receipt is [philly-validation.json](../evidence/native-v1/philly-validation.json). Raw source files, canonical per-job CSV and per-job simulation output remain outside the repository.

## Verified Alibaba run

An origin-preserving A100 first-day slice contains 432 nodes and 1,285 jobs, including 1,284 HP jobs, one Spot job and fractional GPU demand. Two current-engine FIFO runs produced identical result and metrics fingerprints. The aggregate [Alibaba validation receipt](../evidence/native-v1/alibaba-a100-day1-validation.json) records the source hashes, filters, population, evaluation-window allocation and limitations; no raw or per-job data is included.

## Verified Alibaba v2023 QoS run

The v2023 adapter produced 6,100 source-labeled GPU occupancy intervals on 1,213 nodes and 6,212 GPUs: 3,590 `LS → HP` and 2,510 `BE → Spot`. It preserves fractional GPU demand and records 1,829 `Failed`, 4,124 `Running`, and 147 `Succeeded` source phases without reinterpreting them as application success.

Five bounded v2 actions completed the same full canonical trace and passed the available eight-item SLO audit, but all metrics tied. Peak active requested-GPU pressure is only 0.7072% and allocation is 0.1919%, so the trace never exercises contention or preemption. This result is intentionally published as [compatibility evidence](../evidence/native-v2/alibaba-gpu-v2023-qos-full.json), not as an optimization gain.

```powershell
schednav import-alibaba-v2023 `
  --node-info C:\datasets\cluster-trace-gpu-v2023\openb_node_list_gpu_node.csv `
  --pod-info C:\datasets\cluster-trace-gpu-v2023\openb_pod_list_default.csv `
  --output-dir C:\datasets\schednav\alibaba-gpu-v2023-qos
```

## Verified mixed HP/Spot policy evaluation

The representative `GPU-series-2` window for 2024-04-12 uses 122 nodes and 976 GPUs. SchedNav replays 6,501 arrivals from the source origin for carry-in state, then evaluates only the 94 HP and 84 Spot arrivals in seconds `3,628,800..3,715,199`. The peak sampled requested pressure is 1.167008.

Four bounded policies were each simulated twice from the same canonical Trace fingerprint. All repeated result and metrics fingerprints match; all four pass the eight hard constraints in SchedNav Demo SLO v1. The three preemptive policies meet the 80% allocation soft target, but the declared hierarchy leaves them tied and requires human approval. The aggregate [policy-evaluation receipt](../evidence/native-v1/alibaba-gpu-series-2-2024-04-12-policy-evaluation.json) contains the exact metrics, fingerprints and ranking without raw or per-job data.

This receipt and the native-v1/v2/v3 multi-window receipts predate the tenant column and retain their historical Trace fingerprints. Re-importing the same source with the current v2026 adapter produces trace/v2 and therefore requires a new same-trace FIFO baseline; old and new absolute metrics are not mixed.

To reproduce the full import, double simulation, comparison, audit and ranking sequence with a separately downloaded source trace:

```powershell
.\scripts\run_demo_experiment.ps1 `
  -DatasetDirectory C:\datasets\cluster-trace-v2026-spot-gpu `
  -OutputDirectory C:\experiments\schednav-gpu-series-2-2024-04-12
```

## Verified tenant-predictive control run

The current v2026 adapter was also used to build a trace/v2 `GPU-series-2` window with `organization → tenant_id` and pre-evaluation Spot arrivals excluded. It contains 122 nodes, 976 GPUs and 5,876 replayed arrivals; 94 HP and 84 Spot arrivals belong to the evaluated day.

Two independent 28-day-lookback tenant-model trainings at cutoff `3628800` produced identical forecast artifacts. Two closed-loop replays produced identical result and metrics artifacts. The controller passed seven of eight hard SLOs but achieved 75.2975% allocation versus 76.3997% for the exact-trace FIFO baseline, so it was rejected by `allocation-fifo-nondegradation`. The compact [predictive receipt](../evidence/predictive-v1/alibaba-gpu-series-2-2024-04-12-tenant-predictive.json) records model, forecast, quota, feedback, metric and audit fingerprints without redistributing the trace or model state.

## Verified predictive multi-window evaluation

The predictive v1 study reuses dates selected before any predictive replay by the published pressure-by-Spot-share procedure. It excludes the one date with less than the controller's declared 844-hour training minimum, then chronologically freezes six calibration and five holdout windows. FIFO, guarded static, aggregate predictive and tenant predictive arms each run twice from fresh state, for 88 deterministic executions.

No arm passes every hard SLO in all six calibration windows, so the content-addressed pre-holdout selection lock contains no selected arm. On the five diagnostic holdout windows, FIFO and guarded static pass 5/5, tenant predictive passes 1/5 and aggregate predictive passes 0/5. Tenant decomposition improves mean allocation over the aggregate ablation by 0.9792 percentage points, but remains 1.1407 percentage points below FIFO and therefore does not establish predictive-control superiority.

The compact [predictive multi-window receipt](../evidence/predictive-v2/alibaba-gpu-series-2-predictive-multiwindow-v1.json) contains the frozen split, per-window aggregate metrics, SLO outcomes and fingerprint chain. Raw Trace rows, canonical jobs and per-job results remain local. See [predictive-control.md](predictive-control.md) for the execution order and limitations.

## Verified multi-window policy evaluation

The broader `GPU-series-2` study first scanned complete origin-aligned days and retained the 112 windows containing at least 20 HP and 20 Spot jobs. Before any policy simulation, it selected 12 windows by three balanced peak-pressure strata and four balanced Spot-request-share strata per pressure group. Each selected day uses a fixed 30-day warm-up and evaluates five bounded actions twice, for 120 deterministic runs.

Across the 12 windows, the v2 result is two unique FIFO selections, nine unresolved ties and one `no_eligible_policy` outcome. Of the 11 windows with at least one hard-SLO-compliant policy, the best eligible frontier improves allocation over FIFO in seven and matches FIFO in four; none regress because FIFO non-degradation is itself a hard gate. The mean uplift is 0.335 percentage points and the maximum is 1.99 percentage points. This is evidence for safe regime-dependent selection, not a claim that one preemptive profile always wins.

The compact [v2 multi-window receipt](../evidence/native-v2/alibaba-gpu-series-2-multiwindow-30d-v2.json) contains selection metadata, policy aggregates, every window decision and content fingerprints without raw or per-job data. See [multiwindow-evaluation.md](multiwindow-evaluation.md) for methodology, v1 comparison, limitations and reproduction.

## Verified adaptive holdout evaluation

The current v3 study evaluates all 112 eligible `GPU-series-2` windows rather than sampling 12 medoids. A chronological prefix of 67 windows is calibration evidence and the final 45 windows are holdout evaluation. Five bounded actions run twice on every window, producing 1,120 deterministic executions.

The calibration hierarchy selects FIFO as the best fixed action. On holdout, the AgentTeams candidate controller finds a hard-SLO-compliant option in 41/45 windows and covers at least one catalog-frontier action in all 41 feasible windows while evaluating 185 rather than 225 policy-window candidates. Its selected-frontier mean allocation uplift over FIFO ranges from 0.209 to 0.257 percentage points because unresolved ties retain a lower and upper bound; the conservative count is five positive, 36 equal and zero negative outcomes. The three-candidate workload rule uses only 135 evaluations and covers 39/41 formal frontiers, so cost and quality are reported together. Exact frontier-set matches and maximum-allocation candidate coverage remain separately labeled diagnostics.

The [adaptive holdout receipt](../evidence/native-v3/alibaba-gpu-series-2-adaptive-holdout-v3.json) and [all-window receipt](../evidence/native-v3/alibaba-gpu-series-2-all112-v3.json) contain only aggregates, window-level decisions and fingerprints. See [adaptive-holdout-evaluation.md](adaptive-holdout-evaluation.md) for the freeze order, AgentTeams provenance, controller definitions and limitations.

## Evaluation rule

Results from different datasets are separate evidence populations. SchedNav compares policies within the same trace fingerprint and window; it never compares raw metrics across unrelated datasets as though they were controlled experiments. Multi-dataset validation is used to test robustness and expose regime-specific behavior.
