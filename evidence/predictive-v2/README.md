# Predictive multi-window evidence

This directory contains compact public receipts for chronological predictive-control studies. It excludes raw source tables, canonical per-job traces, model checkpoints, per-job simulation results and AgentTeams room logs.

`alibaba-gpu-series-2-predictive-multiwindow-v1.json` records 11 preselected real trace/v2 windows, a six/five calibration-holdout split, four bounded arms, two deterministic repetitions per arm/window, the pre-holdout selection lock and every formal SLO outcome. The receipt preserves the negative result: calibration found no all-window eligible arm, and both predictive arms regress allocation against FIFO on most holdout windows.

After publication, AgentTeams project `proj-20260809-160234` independently checked the frozen windows, arms, 88-run evidence chain and SLO interpretation without re-running simulation. Its Manager retained `approval_pending` and `no_calibration_eligible_arm / selected=[]`; local rooms and task artifacts are intentionally not copied here.
