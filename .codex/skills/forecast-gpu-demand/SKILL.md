---
name: forecast-gpu-demand
description: Build a fingerprinted past-only HP GPU demand forecast and predictive Spot quota plan at an explicit observation cutoff. Use before bounded shadow evaluation or a separately implemented rolling decision. The deterministic SchedNav model, not the LLM, produces numeric forecasts and uncertainty.
---

# Forecast GPU Demand

1. Require a cataloged canonical Trace window, registered controller profile and explicit `cutoff_seconds`. For `tenant-predictive-controller/v1`, require trace/v2, non-empty `tenant_id`, concrete resource pools and a host runtime installed with `.[forecast]`.
2. Call the bridge's `forecast_demand` operation with `run_config_id`, `controller_id` and the cutoff.
3. Return only the `schednav.predictive-observation-bundle/v1` artifact reference and its fingerprints.
4. Verify that the bundle declares:
   - all workload inputs end at the cutoff;
   - jobs submitted after the cutoff are excluded;
   - the full future Trace fingerprint is not exposed;
   - actual future demand is not used for prediction.
5. Summarize the current HP/Spot state, forecast distribution, uncertainty and quota plan without changing any numeric field.

The aggregate profile uses a small seasonal model. The tenant profile uses minute scheduler observations, per-tenant/resource-pool hourly maxima, a 28-day trainable linear Gaussian model, business/time embeddings, daily warm-start training and independent Gaussian pool aggregation. The artifact's controller schema and fingerprint identify which path ran. The LLM must not manufacture a forecast, adjust `mu`, `sigma`, quantiles or `eta`, or read a later Trace window. Forecast accuracy is scored only after the target observation exists.

This skill does not activate or switch a live scheduler policy. Until a state-handoff actuator exists, candidate policies selected from the bundle are shadow-replay hypotheses only.
