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

## Determinism

Ordering is stable by arrival/enqueue order and job ID. Placement is stable by available capacity and node ID. Results, metrics, traces and policies all carry canonical SHA-256 fingerprints. Repeating the same trace and policy produces the same `simulation-result/v1` fingerprint.

The implementation keeps the queue in that declared order and caches free capacity by GPU model to avoid repeated full scans. These are execution optimizations only: placement, preemption, accounting and fingerprint semantics remain unchanged and are covered by deterministic regression tests.

## Current limitations

- no topology, NVLink or network-bandwidth model;
- no CPU/memory admission model;
- no runtime uncertainty or failures;
- no online forecast/MPC loop yet;
- one resource dimension (GPU capacity) is scheduled;
- `spot_guarantee_seconds` is a hard non-preemption boundary in V1, so the guarantee ledger audits enforcement rather than forecast accuracy; the HP queue/JCT trade-off remains measurable;
- Philly's published schema cannot support HP-vs-Spot SLO auditing without an external, justified class mapping.

These limitations are explicit model boundaries, not hidden approximations. Future engine increments should add parity tests and new action fields only when they remain bounded and auditable.
