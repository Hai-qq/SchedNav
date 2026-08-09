# Canonical Trace Contract

SchedNav evaluates every dataset through a versioned canonical contract. `schednav.trace/v1` is the six-column aggregate form; `schednav.trace/v2` adds an explicit tenant dimension for tenant-aware forecasting. Dataset-specific fields are converted once at the ingestion boundary; workload analysis, simulation, comparison and SLO audit consume only the canonical form.

## Files

A canonical trace directory contains:

```text
trace.json
nodes.csv
jobs.csv
```

`trace.json` records the dataset identity, time origin, source provenance, file hashes and a canonical fingerprint. Relative paths cannot escape the trace directory and every file hash is verified before use. It may also declare `evaluation_window_seconds: {start, end}`.

`nodes.csv` has three columns:

```text
node_id,gpu_model,gpu_count
```

`schednav.trace/v1` uses six job columns:

```text
job_id,submit_time_seconds,duration_seconds,gpu_count,service_class,gpu_model
```

`schednav.trace/v2` requires a seventh column:

```text
job_id,submit_time_seconds,duration_seconds,gpu_count,service_class,gpu_model,tenant_id
```

Every v2 row must have a non-empty `tenant_id`; a writer cannot mix tenant-tagged and untagged rows. Trace slicing preserves the source schema version and tenant values.

`gpu_count` may be fractional because real traces can contain fractional GPU requests. `service_class` is exactly `HP` or `Spot`; an adapter must not infer a class that the source data does not provide. `gpu_model=*` allows a job to use any model, while a concrete value restricts placement to matching nodes.

## Provenance rules

- Raw datasets and per-job converted traces stay outside the public repository.
- The manifest records the source URL/version and SHA-256 of source inputs when available.
- Dataset adapters must document every semantic mapping, filter and skipped-record rule.
- A prefix trace starts at the published origin and applies an inclusive arrival cutoff.
- An explicit evaluation trace replays selected arrivals through the inclusive evaluation end. By default replay starts at the published origin; a declared `warmup_start_seconds` may bound carry-in reconstruction for a resource-controlled study. Arrivals from that boundary up to `evaluation_window_seconds.start` are warm-up state: they affect scheduling state but are excluded from the evaluated HP/Spot job population. Allocation is integrated only inside the declared window.
- Explicit evaluation traces cannot contain post-window arrivals and must contain at least one arrival inside the window.
- Content fingerprints cover the normalized files and metadata so AgentTeams passes references rather than large trace payloads.

## Built-in adapters

### Alibaba Spot GPU Trace

`schednav import-alibaba` converts `node_info_df.csv` and `job_info_df.csv` to trace/v2. It preserves published HP/Spot labels, `gpu_request × worker_num`, GPU model and relative submit time, and maps the published `organization` field to `tenant_id`. Optional GPU-model, inclusive submit-time and explicit evaluation-window filters are recorded in provenance.

For a historical window with full carry-in reconstruction, pass both `--evaluation-start-seconds` and `--evaluation-end-seconds`. The importer keeps earlier selected arrivals from the source origin and excludes arrivals after the evaluation end. Supplying only one boundary is rejected.

For predictive experiments, `--exclude-warmup-spot` keeps the pre-evaluation HP history needed for training but removes Spot arrivals before the evaluation boundary. This changes the replay population and therefore the Trace fingerprint; the resulting FIFO baseline must be regenerated from that exact trace. The choice is recorded as `include_warmup_spot=false` in provenance.

For a deliberately bounded carry-in study, also pass `--warmup-start-seconds`. The value must fall between the trace origin and evaluation start and is recorded in provenance. This is an explicit approximation, not equivalent to full-origin replay. Multi-window v1/v2 and the current all-window v3 study fix this boundary at 30 days before each evaluated day and publish that limitation with their evidence. The all-window runner imports the filtered full canonical trace once, then writes provenance-preserving evaluation slices; this changes I/O cost, not window membership or scheduling semantics.

### Alibaba GPU Trace v2023

`schednav import-alibaba-v2023` converts `cluster-trace-gpu-v2023` node and pod tables to trace/v1. It uses only source-published QoS semantics: `LS → HP` and `BE → Spot`; unsupported QoS rows are excluded and counted in provenance. Fractional `gpu_milli` requests are preserved as fractional GPUs, while multi-GPU requests use `num_gpu`.

The source publishes scheduled, deletion and phase fields rather than a counterfactual application runtime. SchedNav therefore records each included `Running`, `Failed` or `Succeeded` row as an observed scheduled-to-deletion occupancy interval and preserves source phase counts in aggregate provenance. A completed simulator job means the replayed occupancy interval ended; it must not be presented as proof that the original application succeeded.

### Microsoft Philly GPU Trace

`schednav import-philly` streams the multi-gigabyte JSON array without loading it all into memory and emits trace/v1. Runtime is the sum of valid scheduling-attempt durations and GPU demand is the maximum GPUs assigned in any valid attempt.

The Philly trace does not publish HP/Spot labels. The command therefore requires an explicit `--service-class HP|Spot` choice and records it as `caller_supplied_constant`. Cross-class SLO claims require a separately justified classification mapping; SchedNav will not invent one.

## Adding a dataset

An adapter only needs to produce the three canonical files and provenance above. The simulator and Agent workflows do not need dataset-specific code. New adapters should include:

1. a source schema fixture;
2. malformed and missing-field tests;
3. an explicit label/mapping policy;
4. source hashes and version metadata;
5. a no-raw-data public-boundary check.

An adapter intended for `tenant-predictive-spot-v1` must additionally provide a source-backed tenant mapping, emit trace/v2 and use concrete resource-pool values rather than `gpu_model=*`.
