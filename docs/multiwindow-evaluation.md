# Multi-window Policy Evaluation

This study tests whether SchedNav's hard-SLO-first control plane can make safe, regime-dependent choices across multiple real workload windows. It is a historical offline counterfactual evaluation, not an online scheduler or a forecasting result.

## Study design

- Dataset: Alibaba `cluster-trace-v2026-spot-gpu`, pinned commit `0d0f3f1efdbf1add6a7bcc63676eafbd1eb11f71`.
- Cluster slice: `GPU-series-2`, 122 nodes and 976 GPUs.
- Eligible population: 112 complete origin-aligned days with at least 20 HP and 20 Spot arrivals.
- Pre-simulation selection: sort by sampled peak active GPU pressure into three balanced strata, then by Spot requested-GPU share into four balanced strata within each pressure group. Select the normalized medoid of each cell, breaking an exact tie by earliest start.
- Evaluated windows: 12 one-day windows selected before policy simulation.
- Carry-in: fixed 30-day warm-up before every evaluated day, followed by drain-to-completion. Allocation integrates only over the evaluated day.
- Actions: `native-fifo`, `native-preemptive-0000`, `native-preemptive-3600`, and `native-preemptive-7200`.
- Repetitions: two fresh-state runs per policy and window, 96 simulations total. All 48 result/metrics pairs reproduced identical fingerprints.
- Decision: apply all eight hard SLOs per window using that window's FIFO baseline, then use the declared allocation → Spot p95 JCT → eviction hierarchy. No cross-window weighted score is created.

The selected dates are 2024-04-03, 2024-04-13, 2024-04-22, 2024-06-08, 2024-06-12, 2024-06-25, 2024-07-05, 2024-07-09, 2024-07-12, 2024-07-21, 2024-08-13 and 2024-08-15.

`native-preemptive-1800` remains a valid single-window profile but is not part of this study's final action space. It exceeded the declared 600-second per-batch feasibility gate in both full-origin and fixed-30-day-warm-up multi-window trials. The receipt records this as an evaluation-resource exclusion, not as an SLO failure or performance result.

## Results

| Policy | Hard-SLO pass | Mean allocation | Mean delta vs FIFO | Positive / equal / negative windows | Mean eviction/run |
|---|---:|---:|---:|---:|---:|
| `native-fifo` | 11/12 | 73.41% | 0.00 pp | 0 / 12 / 0 | 0.00% |
| `native-preemptive-0000` | 7/12 | 74.68% | +1.26 pp | 7 / 3 / 2 | 14.87% |
| `native-preemptive-3600` | 7/12 | 73.76% | +0.34 pp | 7 / 4 / 1 | 16.67% |
| `native-preemptive-7200` | 6/12 | 73.62% | +0.20 pp | 6 / 4 / 2 | 13.52% |

Raw means include windows where a policy fails a hard SLO. In particular, `native-preemptive-0000` has the highest raw mean allocation but fails at least one hard constraint in five windows, so it is not a universal winner.

The formal per-window decisions are:

- 5 unique selections: FIFO in three windows and `native-preemptive-0000` in two;
- 6 `tie_requires_human_approval` outcomes;
- 1 `no_eligible_policy` outcome on 2024-06-25.

Across the 11 windows with at least one eligible policy, the best hard-SLO-compliant frontier has allocation above FIFO in five windows and equal to FIFO in six, with no regressions. Its mean uplift is 0.31 percentage points, median is 0, and maximum is 1.99 percentage points. This non-regression result follows from the explicit FIFO allocation hard gate and retention of FIFO as a candidate; it is not evidence that preemption always improves utilization.

On 2024-06-25, FIFO fails the 3,600-second HP p95 queue limit, while every preemptive action also violates HP and/or Spot constraints. SchedNav returns `no_eligible_policy` instead of hiding the violation or forcing a recommendation.

## Reproduction

Download the source dataset outside the repository, then run from the project root:

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
.\.venv\Scripts\python.exe .\scripts\run_multiwindow_experiment.py `
  --project-root . `
  --dataset-directory C:\datasets\cluster-trace-v2026-spot-gpu `
  --output-directory C:\experiments\schednav-multiwindow-v1 `
  --workers 8

.\.venv\Scripts\python.exe .\scripts\publish_multiwindow_evidence.py `
  --experiment-directory C:\experiments\schednav-multiwindow-v1 `
  --output C:\experiments\schednav-multiwindow-v1-public.json
```

Both commands refuse to overwrite an existing output path. The published [receipt](../evidence/native-v1/alibaba-gpu-series-2-multiwindow-30d-v1.json) contains source hashes, selection and experiment fingerprints, per-policy aggregates and every per-window decision. Raw traces, canonical per-job files, per-job simulation results and logs remain outside Git.

## Evidence fingerprints

- window selection: `a0d6915afd6420b376f0e82f695e6a9f86947cb0b4e2e8ed8092ec89d28413b5`
- multi-window summary: `7f0a36b849c06356148a1820d2e38bbcd683ccbd94c254f5b9e55e2ca50fb8c4`
- experiment manifest: `c438429e390242f2369f060c0ea63380b6aae076892dd28ba9fff5c77a84c240`
- public receipt: `741eecdf2edc17e612023f107f67470d9d6e37d400ed1dc201c9500e705c9173`

## Limitations

- Historical windows expose future arrivals to the offline experiment; there is no rolling forecast/MPC loop yet.
- A 30-day warm-up is a fixed carry-in approximation. Jobs submitted earlier than that boundary are not reconstructed.
- The complete hard-SLO evaluation currently relies on one published source with native HP/Spot labels and one GPU model slice.
- The feasibility exclusion of `native-preemptive-1800` means this is evidence for the declared four-action study, not an exhaustive parameter search.
- The study reports robustness and the eligible frontier; it does not estimate statistical significance or claim a universal policy winner.
