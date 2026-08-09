# SchedNav Demo SLO v1

This SLO applies to policy candidates evaluated on the same canonical Trace fingerprint, evaluation window, population and execution controls. The FIFO reference is always produced by the first-party `native-fifo` policy on that exact Trace; thresholds are not transferred across datasets.

## Hard constraints

Any failed hard constraint eliminates the candidate.

| Metric | Threshold |
|---|---:|
| HP completion rate | 100% |
| HP preempted job count | 0 |
| HP p95 JCT | no more than 1% above same-Trace FIFO |
| HP p95 queue time | ≤ 3,600 s |
| Spot completion rate | 100% |
| Spot eviction rate per run | ≤ 10% |
| Spot guarantee success rate | ≥ 90% |
| GPU allocation rate | no lower than same-Trace FIFO |

Allocation rate is integrated only inside the declared evaluation arrival window. Jobs still drain to completion for completion and JCT evidence, but drain capacity-time is excluded from the utilization denominator.

In the V1 simulator, the Spot guarantee duration is enforced as a hard non-preemption boundary. The guarantee-success metric therefore audits whether that boundary was honored; it is not presented as a forecast-accuracy metric. Longer guarantees can still affect HP queue and JCT, which remain hard SLOs.

The tenant predictive controller also maintains a separate node-weighted feedback ledger with one success per completed guarantee period or job completion and one failure per preemption. That ledger adjusts quota coefficient \(\eta\); it does not replace or redefine the formal run-based SLO metric above.

The relative JCT threshold is:

\[
JCT_{candidate,p95} \leq 1.01 \times JCT_{fifo,p95}
\]

No fixed absolute JCT from one dataset is reused on another dataset.

## Soft objective and ranking

The allocation soft target is 80%. Among candidates passing every hard constraint:

1. maximize allocation rate;
2. if allocation differs by less than one percentage point, minimize Spot p95 JCT;
3. if still tied, minimize Spot eviction rate;
4. preserve an unresolved tie for human approval.

There is no LLM-selected weight vector or undeclared fourth metric.

## Evidence requirements

- valid MetricsReport fingerprint;
- matching Trace/source/window/population for candidate and FIFO baseline;
- completed HP and Spot populations;
- available and consistent preemption, Spot-run and guarantee ledgers;
- exact `schednav.slo-spec/v1` configuration;
- separate simulation from a fresh initial state for every policy.

## Dataset applicability

A dataset without published or justified HP/Spot labels cannot pass this full SLO because the required class-specific metrics are unavailable. Such a dataset can still validate ingestion, placement, completion, JCT, allocation and determinism, but must not be presented as a complete SLO evaluation.

The 3,600-second queue threshold, 90% guarantee target and 10% eviction ceiling are retained as explicit project requirements with the source URL recorded in the machine-readable SLO provenance. Their use does not make metrics from different traces directly comparable.

## Verified 2024-04-12 evaluation

On the representative Alibaba `GPU-series-2` window, the same-Trace FIFO HP p95 JCT is 44,971.1 seconds, so the generated 1% hard limit is 45,420.811 seconds. This value belongs only to this exact Trace fingerprint and population.

| Policy | Allocation | HP p95 JCT | HP p95 queue | Spot p95 JCT | Eviction/run | Guarantee | Hard SLO | 80% soft target |
|---|---:|---:|---:|---:|---:|---:|---|---|
| `native-fifo` | 79.0511% | 44,971.1 s | 0 s | 21,961.1 s | 0% | 100% | 8/8 pass | no |
| `native-preemptive-0000` | 80.5542% | 44,971.1 s | 0 s | 21,961.1 s | 5.6180% | 100% | 8/8 pass | yes |
| `native-preemptive-1800` | 80.7023% | 44,971.1 s | 0 s | 21,961.1 s | 5.6180% | 100% | 8/8 pass | yes |
| `native-preemptive-3600` | 80.7893% | 44,971.1 s | 0 s | 21,961.1 s | 5.6180% | 100% | 8/8 pass | yes |

Every policy also has 100% HP/Spot completion and zero HP preempted jobs. Because all three preemptive policies fall within the strict one-percentage-point allocation band and have identical Spot p95 JCT and eviction rate, the formal result is `tie_requires_human_approval`. See the [aggregate receipt](../evidence/native-v1/alibaba-gpu-series-2-2024-04-12-policy-evaluation.json).

## Verified multi-window evaluation

The current v2 study applied the same SLO independently to 12 preselected `GPU-series-2` daily windows and five bounded actions. FIFO passed all hard constraints in 11/12 windows; the unguarded preemptive action passed 7/12, while the three 9%-eviction-budget actions passed 10/12, 9/12 and 10/12. The study preserved nine ties and one window with no eligible policy rather than forcing a recommendation. Among the 11 feasible windows, the best eligible allocation was higher than FIFO in seven, equal in four and lower in none. See [Multi-window Evaluation](multiwindow-evaluation.md) and the [v2 content-addressed receipt](../evidence/native-v2/alibaba-gpu-series-2-multiwindow-30d-v2.json); the four-action v1 receipt remains historical comparison evidence.

The v3 all-window study applies the same SLO independently to every one of the 112 eligible days. FIFO passes 98/112 windows, the unbudgeted 3,600-second-guarantee preemptive reference passes 60/112, and each of the three 9%-budget guarded profiles passes 80/112. The per-window result contains 35 unique selections, 65 unresolved ties and 12 no-eligible outcomes. On the chronological 45-window holdout, the AgentTeams bounded candidate controller is feasible in 41 windows, covers at least one full-catalog formal-frontier action in all 41 and has no negative conservative allocation uplift versus FIFO. Tied frontiers retain lower and upper allocation bounds for human approval. This is controller-level offline evidence, not a relaxation of any SLO or a universal policy claim. See [Adaptive Holdout Evaluation](adaptive-holdout-evaluation.md).
