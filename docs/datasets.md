# Dataset Support

SchedNav is dataset-neutral after ingestion. Every source is normalized into the canonical trace contract documented in [trace-contract.md](trace-contract.md).

| Dataset | Adapter | Published labels | Redistribution |
|---|---|---|---|
| Alibaba `cluster-trace-v2026-spot-gpu` | `schednav import-alibaba` | HP / Spot | Raw and per-job converted data are not committed. |
| Microsoft Philly GPU Trace | `schednav import-philly` | No HP / Spot label | CC BY 4.0 source is downloaded separately; raw and converted data are not committed. |
| Private or additional public traces | Canonical adapter contract | Adapter-defined and provenance-recorded | Controlled by the source license and data policy. |

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

## Verified mixed HP/Spot policy evaluation

The representative `GPU-series-2` window for 2024-04-12 uses 122 nodes and 976 GPUs. SchedNav replays 6,501 arrivals from the source origin for carry-in state, then evaluates only the 94 HP and 84 Spot arrivals in seconds `3,628,800..3,715,199`. The peak sampled requested pressure is 1.167008.

Four bounded policies were each simulated twice from the same canonical Trace fingerprint. All repeated result and metrics fingerprints match; all four pass the eight hard constraints in SchedNav Demo SLO v1. The three preemptive policies meet the 80% allocation soft target, but the declared hierarchy leaves them tied and requires human approval. The aggregate [policy-evaluation receipt](../evidence/native-v1/alibaba-gpu-series-2-2024-04-12-policy-evaluation.json) contains the exact metrics, fingerprints and ranking without raw or per-job data.

To reproduce the full import, double simulation, comparison, audit and ranking sequence with a separately downloaded source trace:

```powershell
.\scripts\run_demo_experiment.ps1 `
  -DatasetDirectory C:\datasets\cluster-trace-v2026-spot-gpu `
  -OutputDirectory C:\experiments\schednav-gpu-series-2-2024-04-12
```

## Evaluation rule

Results from different datasets are separate evidence populations. SchedNav compares policies within the same trace fingerprint and window; it never compares raw metrics across unrelated datasets as though they were controlled experiments. Multi-dataset validation is used to test robustness and expose regime-specific behavior.
