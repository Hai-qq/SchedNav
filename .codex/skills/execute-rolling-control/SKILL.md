---
name: execute-rolling-control
description: Advance a fingerprint-bound SchedNav rolling GPU scheduling plan through cutoff-safe observations, simulator-backed candidate evaluation, exact runtime state handoff, and final SLO audit. Use for Single-Agent or AgentTeams rolling-horizon experiments; never use it for direct Job-to-GPU placement or with future Trace arrivals exposed.
---

# Execute Rolling Control

1. Require a cataloged rolling run, the study-declared predictive controller, `schednav-demo-slo-v1`, a rolling controller ID, mode (`single_agent`, `multi_agent` or `multi_agent_masked`) and AgentTeams project ID.
2. Call `advance_rolling_policy` with the complete list of already accepted decisions. Start with an empty list.
3. Poll the returned bridge task. If it succeeds with `rolling_checkpoint`, read that artifact and verify:
   - `future_arrivals_visible` is false;
   - the pending observation fingerprint matches the next decision;
   - every completed decision consumed exactly three candidates;
   - the latest candidate evidence and hierarchy were produced by the simulator, not prose.
4. For `single_agent`, delegate candidate formation only to Scheduling Strategist. For `multi_agent`, delegate structured analysis to Workload Analyst, then candidate formation to Scheduling Strategist. For the matched `multi_agent_masked` causal arm, still make both calls but require the Analyst's exact fixed mask and expose only that mask to the Strategist. Use only `deepseek-v4-flash`. Every formal LLM stage must use that Worker's private room, wait for the prior Worker/Manager exchange to become quiescent, acknowledge `/clear` before assignment, record a fingerprinted `context-isolation.json` receipt, and receive exactly one controller observation; never batch different holdout windows into one conversational context. After dispatch, audit the Worker Handle-agent-query log: the sole user input must be the exact assignment, not a Matrix `[Chat messages since your last reply]` aggregate. Keep this receipt separate from AgentTeams `meta.json`, which may be normalized when a Worker acknowledges a task.
5. Submit exactly three distinct registered action IDs including the pending observation's `required_candidate_action_id`. Attach normalized AgentTeams role receipts, one bounded reason code and observed model usage. Do not include metrics from the hidden execution interval.
6. Append the decision and call `advance_rolling_policy` again. Repeat until it returns completed metrics, FIFO baseline, formal SLO audit and rolling control report.
7. Preserve Simulation Agent and SLO Auditor task receipts for the deterministic execution and audit stages. Export task metadata plus the fingerprinted isolation receipts proving private-room, clear-before-assignment, single-observation isolation; a stage without that proof is not evidence. The Manager may recommend only from completed evidence and must retain human approval.
8. Publish the compact rolling-ablation evidence before final audit. Bind the final SLO Auditor normalized-stage byte hash into the Manager decision, then validate both task artifacts with `scripts/publish_rolling_agentteams_closeout.py`. A negative superiority gate must remain `not_established`; do not convert a safe fallback recommendation into an Agent-performance claim.

The bridge deterministically replays accepted choices only to reveal the next observation. The final completed replay is one simulator session: queue order, allocations, remaining work, preemption/guarantee ledgers, predictor state and quota feedback all persist across cutoffs. The v3 evaluator uses a calibrated-P90 decision scenario plus a cutoff-safe recent-history stress replay; hard eligibility comes from the decision scenario and stress evidence is used only after the declared SLO hierarchy is tied. Only the explicitly labeled post-hoc oracle may use actual future arrivals.
