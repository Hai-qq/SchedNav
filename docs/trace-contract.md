# Canonical Trace Contract

SchedNav evaluates every dataset through `schednav.trace/v1`. Dataset-specific fields are converted once at the ingestion boundary; workload analysis, simulation, comparison and SLO audit consume only the canonical form.

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

`jobs.csv` has six columns:

```text
job_id,submit_time_seconds,duration_seconds,gpu_count,service_class,gpu_model
```

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

`schednav import-alibaba` converts `node_info_df.csv` and `job_info_df.csv`. It preserves published HP/Spot labels, `gpu_request × worker_num`, GPU model and relative submit time. Optional GPU-model, inclusive submit-time and explicit evaluation-window filters are recorded in provenance.

For a historical window with full carry-in reconstruction, pass both `--evaluation-start-seconds` and `--evaluation-end-seconds`. The importer keeps earlier selected arrivals from the source origin and excludes arrivals after the evaluation end. Supplying only one boundary is rejected.

For a deliberately bounded carry-in study, also pass `--warmup-start-seconds`. The value must fall between the trace origin and evaluation start and is recorded in provenance. This is an explicit approximation, not equivalent to full-origin replay. The multi-window v1 study fixes this boundary at 30 days before each evaluated day and publishes that limitation with its evidence.

### Microsoft Philly GPU Trace

`schednav import-philly` streams the multi-gigabyte JSON array without loading it all into memory. Runtime is the sum of valid scheduling-attempt durations and GPU demand is the maximum GPUs assigned in any valid attempt.

The Philly trace does not publish HP/Spot labels. The command therefore requires an explicit `--service-class HP|Spot` choice and records it as `caller_supplied_constant`. Cross-class SLO claims require a separately justified classification mapping; SchedNav will not invent one.

## Adding a dataset

An adapter only needs to produce the three canonical files and provenance above. The simulator and Agent workflows do not need dataset-specific code. New adapters should include:

1. a source schema fixture;
2. malformed and missing-field tests;
3. an explicit label/mapping policy;
4. source hashes and version metadata;
5. a no-raw-data public-boundary check.
