# SchedNav-native validation evidence

This directory contains aggregate, public evidence produced by the first-party `schednav-sim` engine. It does not contain raw traces, canonical per-job CSV files, per-job simulation results or logs.

`alibaba-a100-day1-validation.json` records the labeled A100 first-day validation and its two-run determinism. `alibaba-gpu-series-2-2024-04-12-policy-evaluation.json` records the mixed HP/Spot four-policy experiment, eight hard-SLO audits and approval-pending ranking. `alibaba-gpu-series-2-multiwindow-30d-v1.json` records the pre-simulation-stratified 12-window study, 96 deterministic runs, per-window SLO decisions and aggregate robustness without raw or per-job records. `philly-validation.json` records source identity, hashes, conversion counts, the origin-preserving slice and two-run determinism. `philly-hp-1000-fifo-metrics.json` is the canonical aggregate MetricsReport for the Philly slice.

The Microsoft Philly source does not publish HP/Spot labels. The adapter was invoked with an explicit constant `HP` mapping, recorded in provenance. Therefore this run validates ingestion, fractional/integral GPU accounting, placement, completion, evaluation-window allocation and determinism; it does not validate Spot eviction, guarantee rate or HP-vs-Spot SLO trade-offs.
