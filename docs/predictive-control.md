# Predictive Spot Control

SchedNav contains a complete history-only predictive quota loop. It observes HP demand, trains a probabilistic model, converts a conservative forecast into executable Spot admission quotas, applies eviction/queue feedback, and records every decision for later SLO audit. The Agent chooses only a registered controller and policy; numeric forecasts, quotas, placement and preemption remain deterministic code paths.

The implementation is an independent MIT-licensed realization of the predictive scheduling design described in the [public paper](https://arxiv.org/abs/2509.11134). No external scheduler source tree, model checkpoint or dataset row is included in this repository.

## Controller profiles

| Profile | Purpose | Data contract | Runtime |
|---|---|---|---|
| `predictive-spot-v1` | Small dependency-free aggregate baseline | `schednav.trace/v1` or v2 | Python standard library |
| `tenant-predictive-spot-v1` | Full tenant/resource-pool probabilistic controller | `schednav.trace/v2`, non-empty `tenant_id`, concrete `gpu_model` | PyTorch and `chinese-calendar` |

The tenant profile is the reference profile for predictive-control experiments. The aggregate profile remains useful for fast contract tests and ablations; its results must not be described as model parity with the tenant profile.

## Capability coverage

| Published predictive-control mechanism | SchedNav implementation |
|---|---|
| Per-organization HP demand recorder | Per-`(resource_pool, tenant_id)` scheduler observation every minute |
| Hourly maximum, 28-day input, four-hour forecast | Same cadence and horizons, aligned at five-minute training strides |
| Workday/weekday/hour plus three business categories | China calendar features plus resource-pool/cluster/tenant embeddings |
| Moving-average decomposition, attribute attention, linear mean/scale heads | `tenant-linear-gaussian-v1` first-party trainable network |
| Gaussian likelihood and tenant uncertainty aggregation | Non-zero-target Gaussian NLL; means sum and independent variances sum per pool |
| Daily training with seven-day validation and early stopping | Deterministic daily warm start with the same declared epochs, batch, patience and learning-rate profile |
| P90 HP reservation and 1/2/4-hour Spot quota | Per-pool quantiles, horizon minimum, \(\eta\), and runtime inventory cap |
| Four-hour success/failure feedback | Node-weighted periodic guarantee, completion and preemption events with bounded per-pool \(\eta\) |
| Scheduler execution | Event-driven SchedNav queue, placement, guarantee and preemption engine |
| Multi-agent orchestration | Registered AgentTeams Manager/Worker Skills and bounded bridge operations |

The algorithmic behavior is internalized rather than wrapping or importing another scheduler. This is control-contract parity, not a claim that model checkpoints or every floating-point result are interchangeable with another implementation. Category cardinalities are built from the observed prefix instead of being fixed to one dataset, and canonical `gpu_count` represents total job demand instead of a per-worker value. Two implementation details are intentionally safer: the early-stopping best state is retained in memory instead of writing an intermediate checkpoint into the repository, and the low-eviction feedback equation is zero-safe and clamps \(\eta\) to declared bounds. Concrete placement remains a simulator responsibility and is never exposed to the LLM.

Install the optional runtime with:

```powershell
python -m pip install -e ".[forecast]"
# Windows project runtime: .\scripts\setup_runtime.ps1 -Forecast
```

Imports remain lazy, so ordinary simulation and analysis do not require PyTorch.

## Tenant-aware observation and training

The default profile at `configs/controllers/tenant-predictive-spot-v1.json` fixes the following behavior:

- sample scheduler-observed running HP requested GPUs every 60 seconds;
- keep one series per `(resource_pool, tenant_id)` and retain pool/cluster/tenant categorical metadata;
- aggregate each rolling input into hourly maxima aligned at five-minute training strides;
- use 672 input hours (28 days) to predict the next four hourly demand values;
- reserve the most recent 168 hours of fully observed targets for validation;
- warm-start training once per day, up to ten epochs, with deterministic shuffling, early stopping after three stale epochs and the declared learning-rate schedule;
- ignore zero targets in the Gaussian likelihood, matching the sparse-demand training contract;
- use China workday, weekday and hour-of-day calendar features.

The default first training point needs at least

\[
672\text{ h lookback}+168\text{ h validation}+4\text{ h target}=844\text{ h}
\]

of past-only observations. Until that boundary is reached, `history_ready=false` and the tenant profile emits zero admission quota rather than inventing a forecast.

The first-party `tenant-linear-gaussian-v1` network contains:

1. repeated-edge moving-average decomposition into cycle and trend components;
2. embeddings for resource pool, cluster and tenant, followed by self-attention across those business attributes;
3. embeddings for workday, weekday and hour;
4. shared linear cycle, trend and uncertainty heads;
5. a Softplus uncertainty output and Gaussian negative log-likelihood.

Training keeps the best validation state in memory, supports daily warm starts inside one replay, and emits a SHA-256 model fingerprint over the exact model state and structured metadata. CPU execution is intentionally deterministic for reproducible evidence.

## Forecast aggregation and quota

For tenant series \(i\) in resource pool \(v\), the model emits \((\mu_{i,k},\sigma_{i,k})\) for future hour \(k\). SchedNav uses the declared independent-series assumption:

\[
\mu_{v,k}=\sum_i\mu_{i,k},
\qquad
\sigma_{v,k}=\sqrt{\sum_i\sigma_{i,k}^{2}}.
\]

For pool capacity \(C_v\), guarantee probability \(p\), and Normal quantile \(z_p\), reserved HP demand and forecast free capacity are

\[
R_{v,k}=\operatorname{clip}
\left(\left\lfloor\mu_{v,k}+z_p\sigma_{v,k}\right\rfloor,0,C_v\right),
\]

\[
F_v(t,h)=\min_{1\le k\le h}(C_v-R_{v,k}).
\]

The controller then computes

\[
Q_v(t,h)=\left\lfloor
\operatorname{clip}\left(\eta_{v,t}F_v(t,h),0,C_v\right)
\right\rfloor.
\]

With the default runtime inventory cap, the executable value is also bounded by current idle GPUs plus already-running Spot GPUs. A queued Spot job is admitted only when its requested GPUs plus the currently running Spot allocation in its resource pool fit the quota for its guarantee horizon. A lower later quota never kills a running job by itself.

## Guarantee feedback

The tenant controller recomputes quota every 300 seconds and evaluates feedback over the preceding four hours. Feedback events are scoped per resource pool and weighted by allocated node count:

- every completed guarantee period records success and advances that run's feedback checkpoint;
- job completion records success, including a final partial period;
- preemption records failure;
- a restarted Spot job begins a new checkpoint sequence.

This controller-feedback ledger is distinct from the formal V1 SLO ledger. The latter remains run-based so existing published policy evidence and thresholds keep one stable metric definition.

Let \(e^*=1-p\) and let \(e_t\) be the recent weighted eviction rate. The bounded correction is

\[
\eta_{t+1}=\operatorname{clip}
\left(\eta_t\frac{e^*}{e_t},\eta_{\min},\eta_{\max}\right)
\quad\text{when }e_t\ge1.5e^*,
\]

and, when \(e_t\le0.5e^*\) and the oldest queued Spot work has waited more than 3,600 seconds,

\[
\eta_{t+1}=\operatorname{clip}
\left(\eta_t\left(1.5-\frac{e_t}{e^*}\right),
\eta_{\min},\eta_{\max}\right).
\]

The checked-in profile bounds \(\eta\) to `[0.25, 1.25]`. Every decision records the recent event weight, observed eviction rate, old/new \(\eta\), adjustment reason and per-pool quotas.

## Information boundary

There are two entry points with different state sources:

1. `forecast-demand` creates a one-cutoff artifact from only jobs submitted no later than the cutoff. It does not expose the full Trace fingerprint. Because a file Trace has no live queue snapshot, this command reconstructs submit-to-submit-plus-duration occupancy as an explicitly labeled offline approximation.
2. `simulate-predictive` advances the simulator and controller together. Each update receives actual current queues, allocations, per-tenant running HP demand, per-pool idle capacity, running Spot inventory and observed guarantee events. The future arrival list is never passed to the controller. Realized future HP demand is used only after its target time for forecast diagnostics.

The tenant profile rejects aggregate v1 traces, missing tenant IDs and wildcard resource pools rather than silently collapsing the dimensions. The Alibaba v2026 adapter maps the published `organization` field to `tenant_id`; `--exclude-warmup-spot` can build an HP-history warm-up without future-window Spot carry-in.

Leakage regressions create two traces with identical prefixes and different futures and require identical cutoff bundles. Repeated real-window training and replay must also produce identical content hashes.

## Structured contracts

| Schema | Purpose |
|---|---|
| `schednav.tenant-predictive-controller/v1` | Tenant model, training, quota and feedback parameters. |
| `schednav.observation-snapshot/v1` | Past observation history and current scheduler state. |
| `schednav.demand-forecast/v1` | Per-pool \(\mu\), \(\sigma\), P90 quantile and model evidence. |
| `schednav.spot-quota-plan/v1` | Aggregate and per-pool executable quotas by guarantee horizon. |
| `schednav.predictive-observation-bundle/v1` | Agent-safe cutoff artifact and information-boundary receipt. |
| `schednav.predictive-control-report/v1` | Rolling decisions, training, forecast diagnostics, feedback events, \(\eta\) and quota ranges. |
| `schednav.predictive-run/v1` | Host-bridge receipt linking trace, policy, controller, result and metrics. |

Forecast MAE, WAPE, mean error, P90 coverage and pinball loss are diagnostics. They are never converted into an undeclared SLO or an LLM-weighted score.

## AgentTeams mapping

The predictive bridge profile is `configs/agentteams/host-bridge-predictive-v1.json`. It registers both controller profiles and the `tenant-predictive-local` v2 run configuration, and exposes only:

- `forecast_demand` to Workload Analyst;
- registered policy/controller selection to Scheduling Strategist;
- `simulate_predictive_policy` to Simulation Agent;
- `audit_slo` to SLO Auditor;
- deterministic comparison/ranking and human approval to Manager.

AgentTeams passes artifact references, schema versions and fingerprints. It cannot submit a forecast array, modify \(\mu\), \(\sigma\), quantiles or \(\eta\), inject a job/node/GPU ID, or execute arbitrary placement code.

## Verified real-window evidence

The checked-in [tenant-predictive receipt](../evidence/predictive-v1/alibaba-gpu-series-2-2024-04-12-tenant-predictive.json) records two independent cutoff forecasts and two complete closed-loop replays on the same real `GPU-series-2` trace/v2 window. Both forecast artifacts are identical, and both result/metrics pairs are identical.

At cutoff `3628800`, four observed HP tenant series produced a four-hour P90 forecast and 1/2/4-hour quotas of 189/188/188 GPUs. The closed loop executed 61,920 minute observations and 288 quota decisions. Its formal metrics were:

| Metric | Tenant predictive | Compatible FIFO |
|---|---:|---:|
| HP completion | 100% | 100% |
| HP p95 JCT | 44,971.1 s | 44,971.1 s |
| HP p95 queue | 0 s | 0 s |
| Spot completion | 100% | 100% |
| Spot p95 JCT | 28,788.15 s | 21,961.1 s |
| Spot eviction/run | 0% | 0% |
| Spot guarantee success | 100% | 100% |
| Allocation | 75.2975% | 76.3997% |

Seven of eight hard constraints pass. `allocation-fifo-nondegradation` fails, so the controller is rejected for this window. This proves that the trainable prediction, quota, feedback, replay and audit path works deterministically; it does not prove performance superiority. The trace fingerprint differs from the earlier aggregate-controller shadow run, so their absolute metrics must not be compared as a controlled experiment.

## Commands

```powershell
schednav import-alibaba `
  --node-info C:\datasets\gpu-trace\node_info_df.csv `
  --job-info C:\datasets\gpu-trace\job_info_df.csv `
  --output-dir C:\datasets\schednav\tenant-window `
  --gpu-model GPU-series-2 `
  --evaluation-start-seconds 3628800 `
  --evaluation-end-seconds 3715199 `
  --exclude-warmup-spot

schednav forecast-demand `
  --trace C:\datasets\schednav\tenant-window\trace.json `
  --controller configs\controllers\tenant-predictive-spot-v1.json `
  --cutoff-seconds 3628800 `
  --output C:\experiments\schednav\tenant-forecast.json

schednav simulate-predictive `
  --trace C:\datasets\schednav\tenant-window\trace.json `
  --policy configs\policies\native-preemptive-g3600-b09-d0000.json `
  --controller configs\controllers\tenant-predictive-spot-v1.json `
  --result C:\experiments\schednav\tenant-result.json `
  --metrics C:\experiments\schednav\tenant-metrics.json
```

## Current boundary

- This is an online-shaped control loop evaluated by historical shadow replay, not a Kubernetes/Slurm actuator or production deployment.
- The closed loop changes Spot admission quota under one fixed registered policy. Outer rolling policy switching and state handoff are separate work.
- The independent-Gaussian tenant aggregation is a declared modeling assumption whose calibration must be measured per dataset.
- A cold-start window shorter than 844 hours cannot train the default tenant profile.
- Persistent model serving, process-restart recovery, live telemetry collection, deployment rollback and cluster failure handling are not implemented.
- Every policy still requires a compatible FIFO baseline and the declared hard-SLO-first audit. Prediction alone is not evidence of improvement.
