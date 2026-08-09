# Multi-window Policy Evaluation

This study tests whether SchedNav's hard-SLO-first control plane can make safe, regime-dependent choices across multiple real workload windows. It is a historical offline counterfactual evaluation, not an online scheduler or a forecasting result.

## Study design

- Dataset: Alibaba `cluster-trace-v2026-spot-gpu`, pinned commit `0d0f3f1efdbf1add6a7bcc63676eafbd1eb11f71`.
- Cluster slice: `GPU-series-2`, 122 nodes and 976 GPUs.
- Eligible population: 112 complete origin-aligned days with at least 20 HP and 20 Spot arrivals.
- Pre-simulation selection: sort by sampled peak active GPU pressure into three balanced strata, then by Spot requested-GPU share into four balanced strata within each pressure group. Select the normalized medoid of each cell, breaking an exact tie by earliest start.
- Evaluated windows: 12 one-day windows selected before policy simulation.
- Carry-in: fixed 30-day warm-up before every evaluated day, followed by drain-to-completion. Allocation integrates only over the evaluated day.
- Actions: `native-fifo`, unguarded `native-preemptive-0000`, and three guarded actions with a 9% Spot eviction budget plus 0/900/1,800-second HP preemption delay.
- Repetitions: two fresh-state runs per policy and window, 120 simulations total. All 60 result/metrics pairs reproduced identical fingerprints.
- Decision: apply all eight hard SLOs per window using that window's FIFO baseline, then use the declared allocation → Spot p95 JCT → eviction hierarchy. No cross-window weighted score is created.

The selected dates are 2024-04-03, 2024-04-13, 2024-04-22, 2024-06-08, 2024-06-12, 2024-06-25, 2024-07-05, 2024-07-09, 2024-07-12, 2024-07-21, 2024-08-13 and 2024-08-15.

The eviction controller checks the cap over all replayed Spot runs and separately over evaluation-population Spot runs. This prevents warm-up jobs from diluting the audited budget. The unguarded immediate-preemption action remains in the portfolio as a trade-off reference, not as a safe default.

## Results

| Policy | Hard-SLO pass | Mean allocation | Mean delta vs FIFO | Positive / equal / negative windows | Mean eviction/run |
|---|---:|---:|---:|---:|---:|
| `native-fifo` | 11/12 | 73.41% | 0.00 pp | 0 / 12 / 0 | 0.00% |
| `native-preemptive-0000` | 7/12 | 74.68% | +1.26 pp | 7 / 3 / 2 | 14.87% |
| `native-preemptive-budget09-d0000` | 10/12 | 73.26% | -0.16 pp | 8 / 3 / 1 | 1.81% |
| `native-preemptive-budget09-d0900` | 9/12 | 73.27% | -0.14 pp | 5 / 4 / 3 | 0.98% |
| `native-preemptive-budget09-d1800` | 10/12 | 73.32% | -0.09 pp | 5 / 5 / 2 | 0.98% |

Raw means include windows where a policy fails a hard SLO. In particular, `native-preemptive-0000` has the highest raw mean allocation but fails at least one hard constraint in five windows, so it is not a universal winner.

The formal per-window decisions are:

- 2 unique selections, both FIFO;
- 9 `tie_requires_human_approval` outcomes;
- 1 `no_eligible_policy` outcome on 2024-06-25.

Across the 11 windows with at least one eligible policy, the best hard-SLO-compliant frontier has allocation above FIFO in seven windows and equal to FIFO in four, with no regressions. Its mean uplift is 0.335 percentage points, median is effectively zero, and maximum is 1.99 percentage points. Compared with the original four-action study, this adds two positive-frontier windows while preserving the same maximum and no-regression property. The improvement comes from a broader admissible portfolio, not from one universal winner.

On 2024-06-25, FIFO fails the 3,600-second HP p95 queue limit, while every preemptive action also violates HP and/or Spot constraints. SchedNav returns `no_eligible_policy` instead of hiding the violation or forcing a recommendation.

## Reproduction

Download the source dataset outside the repository, then run from the project root:

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
.\.venv\Scripts\python.exe .\scripts\run_multiwindow_experiment.py `
  --project-root . `
  --dataset-directory C:\datasets\cluster-trace-v2026-spot-gpu `
  --output-directory C:\experiments\schednav-multiwindow-v2 `
  --action-space configs\action_spaces\native-multiwindow-v2.json `
  --workers 8

.\.venv\Scripts\python.exe .\scripts\publish_multiwindow_evidence.py `
  --experiment-directory C:\experiments\schednav-multiwindow-v2 `
  --output C:\experiments\schednav-multiwindow-v2-public.json
```

Both commands refuse to overwrite an existing output path. The published [v2 receipt](../evidence/native-v2/alibaba-gpu-series-2-multiwindow-30d-v2.json) contains source hashes, selection and experiment fingerprints, per-policy aggregates and every per-window decision. The original [v1 receipt](../evidence/native-v1/alibaba-gpu-series-2-multiwindow-30d-v1.json) remains available for comparison. Raw traces, canonical per-job files, per-job simulation results and logs remain outside Git.

## Evidence fingerprints

- window selection: `a0d6915afd6420b376f0e82f695e6a9f86947cb0b4e2e8ed8092ec89d28413b5`
- multi-window summary: `f70c915b27d80bd7a7d3bd56a207e6c8dd3a92676dac0099dee5eea90ccd167b`
- experiment manifest: `3e39b7a029f76471e7e5a77b23b38af4bb9082a4d561f80fc159d36c42f8243d`
- public receipt: `76ab0a10d3b66913ed212f9712908309e0c77ededf1d6e94af07e6231e63d3f2`

## Limitations

- Historical windows expose future arrivals to the offline experiment; there is no rolling forecast/MPC loop yet.
- A 30-day warm-up is a fixed carry-in approximation. Jobs submitted earlier than that boundary are not reconstructed.
- The contention study currently relies on one published source with native HP/Spot labels and one GPU model slice. The second source-semantic HP/Spot trace is too underloaded to exercise scheduling trade-offs.
- The five profiles are a finite hand-designed portfolio, not an exhaustive parameter search.
- The study reports robustness and the eligible frontier; it does not estimate statistical significance or claim a universal policy winner.
