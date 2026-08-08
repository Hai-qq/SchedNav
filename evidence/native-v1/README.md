# SchedNav-native validation evidence

This directory contains aggregate, public evidence produced by the first-party `schednav-sim` engine. It does not contain raw traces, canonical per-job CSV files, per-job simulation results or logs.

`alibaba-a100-day1-validation.json` records the labeled A100 first-day validation and its two-run determinism. `philly-validation.json` records source identity, hashes, conversion counts, the origin-preserving slice and two-run determinism. `philly-hp-1000-fifo-metrics.json` is the canonical aggregate MetricsReport for the Philly slice.

The Microsoft Philly source does not publish HP/Spot labels. The adapter was invoked with an explicit constant `HP` mapping, recorded in provenance. Therefore this run validates ingestion, fractional/integral GPU accounting, placement, completion, evaluation-window allocation and determinism; it does not validate Spot eviction, guarantee rate or HP-vs-Spot SLO trade-offs.
