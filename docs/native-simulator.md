# SchedNav Simulator

`schednav-sim` is SchedNav's first-party deterministic discrete-event GPU scheduling engine. It is implemented under `src/schednav/native_simulator.py` and distributed under the repository MIT License.

## Ownership boundary

Agents select only a cataloged high-level `SimulationPolicy`. The engine owns queue ordering, resource accounting, preemption and concrete node allocation. No Agent or LLM supplies `job_id`, `node_id`, `gpu_id` or placement decisions.

## V1 model

- heterogeneous nodes with integer physical GPU capacity;
- integral or fractional per-job GPU demand;
- GPU-model affinity or wildcard eligibility;
- gang-style allocation across one or more eligible nodes;
- deterministic best-fit placement with stable tie breaking;
- FIFO and HP-first preemptive scheduling profiles;
- Spot guarantee boundaries;
- optional HP preemption delay and rolling evaluation-population Spot eviction budget;
- checkpoint rollback and preemption overhead;
- drain-to-completion execution;
- per-job, run-start, guarantee and preemption ledgers.

The simulator integrates allocated GPU-seconds exactly between discrete events. Allocation rate is allocated GPU-seconds divided by physical GPU-seconds inside the explicit evaluation window, or the arrival window when no explicit boundary is declared. Jobs still drain to completion after the window, but drain capacity-time is excluded from the utilization denominator.

When a trace declares an explicit window, available pre-window arrivals are replayed as warm-up. They can occupy GPUs or complete during the evaluated interval, but they are not counted in HP/Spot completion, JCT, queue, eviction, Spot-run or guarantee populations. Cluster allocation before the evaluation start is recorded separately as `warmup_allocated_gpu_seconds`. A Spot eviction is a preemption event, and the eviction rate is events for evaluation-population Spot jobs divided by their explicit run starts. A trace may use full-origin carry-in or publish a bounded warm-up boundary; the latter is recorded as an evaluation limitation.

## Policy contract

`schednav.simulation-policy/v1` exposes:

- `scheduler`: `fifo` or `priority_preemptive`;
- `spot_guarantee_seconds`;
- `checkpoint_interval_seconds`;
- `preemption_overhead_seconds`;
- optional `hp_preemption_delay_seconds` (default `0`);
- optional `spot_eviction_budget_rate` in `[0, 1]` (default unset);
- optional `preemption_victim_strategy`: `longest_remaining` (default) or `lowest_checkpoint_loss`;
- fixed `placement_strategy=deterministic_best_fit`.

Default-valued optional fields are omitted from canonical serialization, preserving existing v1 policy fingerprints. The eviction controller enforces the declared cap over all replayed Spot runs and, separately, over the evaluation-window Spot population used by `spot_eviction_rate_per_run`. This prevents carry-in activity from diluting the audited budget or becoming an unbounded source of evictions. The loss-aware victim rule deterministically minimizes checkpoint rollback at an HP preemption event, then uses remaining work and stable source identifiers as tie-breakers; the Agent never selects the concrete victim.

The current v3 multi-window action space fixes execution controls and provides five curated profiles with a 3,600-second Spot guarantee: FIFO, an unbudgeted preemptive reference, two 9% eviction-budget profiles with 0/900-second HP delays, and one 9%-budget loss-aware victim profile. The v2 action space remains historical reproduction evidence. Arbitrary cross-products and direct placement fields are not Agent actions.

## Predictive control mode

`simulate-predictive` attaches one registered stateful controller to the same event engine. Controller updates become deterministic simulator events. The aggregate profile receives current outstanding HP demand, queued Spot demand and running Spot allocation. The tenant profile additionally receives scheduler-observed running HP GPUs per tenant/resource pool, per-pool idle and running Spot inventory, per-pool backlog and maximum queue wait. Neither profile receives jobs whose submit events have not occurred.

`tenant-predictive-spot-v1` observes every minute, forms hourly maxima at five-minute-aligned training strides, uses a 28-day input and seven-day validation boundary, retrains the probabilistic tenant model daily, forecasts four hours and reserves the P90 HP-demand quantile independently per resource pool. It admits Spot work under the minimum predicted free capacity across the requested guarantee horizon, bounded by current idle plus running Spot inventory. Each completed guarantee period and job completion is a node-weighted feedback success; a preemption is a feedback failure. Those observed events adjust a bounded per-pool quota coefficient every five minutes. Running jobs are not evicted merely because a later prediction lowers the quota.

The tenant profile requires trace/v2, source-backed tenant IDs and concrete GPU resource pools. This is validated before training or replay so an aggregate trace cannot silently masquerade as tenant-aware evidence. The smaller `predictive-spot-v1` profile remains available as a dependency-free aggregate baseline.

Predictive runs add `schednav.predictive-control-report/v1` evidence to the ordinary simulation result and a compact summary to the metrics report. The report stores per-cutoff decisions only inside the declared evaluation window, records the total minute observations, daily training state/model fingerprint, per-pool feedback totals, quota and \(\eta\) ranges. Forecast targets are scored only after their observation time. Static simulation remains byte-compatible: when no controller is attached, no predictive field is serialized and existing policy/result fingerprints retain their original semantics.

The exact estimator, quota equation, leakage boundary, AgentTeams mapping and commands are documented in [Predictive Spot Control](predictive-control.md).

## Determinism

Ordering is stable by arrival/enqueue order and job ID. Placement is stable by available capacity and node ID. Results, metrics, traces, policies, controllers and trained tenant-model states carry SHA-256 fingerprints. The trainable profile fixes its CPU seed, deterministic data order and daily warm-start schedule. Repeating the same trace, policy and controller produces the same forecast, result and metrics content.

The implementation keeps the queue in that declared order and caches free capacity by GPU model to avoid repeated full scans. These are execution optimizations only: placement, preemption, accounting and fingerprint semantics remain unchanged and are covered by deterministic regression tests.

## Current limitations

- no topology, NVLink or network-bandwidth model;
- no CPU/memory admission model;
- no runtime uncertainty or failures;
- no live Kubernetes/Slurm state adapter or actuator; predictive control currently runs as cutoff-safe offline replay/shadow evaluation;
- no outer rolling-horizon policy switching or simulator-state handoff between policy profiles yet;
- one resource dimension (GPU capacity) is scheduled;
- `spot_guarantee_seconds` is a hard non-preemption boundary in V1, so the guarantee ledger audits enforcement rather than forecast accuracy; the HP queue/JCT trade-off remains measurable;
- Philly's published schema cannot support HP-vs-Spot SLO auditing without an external, justified class mapping.

These limitations are explicit model boundaries, not hidden approximations. Future engine increments should add parity tests and new action fields only when they remain bounded and auditable.
