# 研究结果与证据边界

第一方内核已在两个数据提供方、三个真实 GPU Trace 版本上运行。[Alibaba 12-window v2 evaluation](../evidence/native-v2/alibaba-gpu-series-2-multiwindow-30d-v2.json) 固化了 12 个预先分层窗口、5 个策略、每策略每窗口 2 次重复，共 120 次确定性仿真的汇总证据；[Alibaba v2023 QoS receipt](../evidence/native-v2/alibaba-gpu-v2023-qos-full.json) 验证了第二套源生服务等级语义、fractional GPU、仿真、SLO 与并列保留；[Alibaba mixed HP/Spot policy evaluation](../evidence/native-v1/alibaba-gpu-series-2-2024-04-12-policy-evaluation.json) 记录了一个带 warm-up carry-in 的代表性窗口；[Philly validation receipt](../evidence/native-v1/philly-validation.json) 验证了另一提供方的 ingestion 与确定性。

该 Philly 验证只覆盖 ingestion、placement、completion、JCT、allocation 和 determinism。由于源数据没有 HP/Spot 标签，Spot eviction、guarantee 和 HP-vs-Spot SLO 明确保持未验证。

代表性 Alibaba 窗口的四个策略全部通过 8 项硬 SLO。三个抢占策略达到 80% allocation 软目标，但在严格 1 个百分点 allocation tie band 内，且 Spot p95 JCT 与 eviction rate 相同，因此结果保持 `tie_requires_human_approval`，未添加隐藏的第四排序指标。

可训练的 tenant-predictive 路径也已在真实 `GPU-series-2` trace/v2 窗口上重复两次：两个 cutoff forecast 以及两组完整 result/metrics 分别同哈希。该控制器通过 7/8 项硬 SLO，但 allocation 为 75.30%，低于兼容 FIFO 的 76.40%，因此被硬约束淘汰。这个[精简证据回执](../evidence/predictive-v1/alibaba-gpu-series-2-2024-04-12-tenant-predictive.json)证明预测、quota、反馈、仿真与审计链路可执行且可复现，不证明性能优于 FIFO。

进一步的[预测多窗口回执](../evidence/predictive-v2/alibaba-gpu-series-2-predictive-multiwindow-v1.json)复用预先分层选出的真实日期，剔除唯一不足 844 小时训练历史的窗口，并按时间顺序切成 6 个 calibration 与 5 个 holdout。四条对照链、每窗两次运行共 88 次仿真均确定性复现。校准阶段没有任何 arm 在 6/6 窗口通过全部硬 SLO，因此 selection lock 为空；这个结论在运行 holdout 前已固化。holdout 中 FIFO 与 guarded-static 均为 5/5，tenant-predictive 为 1/5，aggregate-predictive 为 0/5。tenant 分解优于 aggregate，但两者 allocation 均低于 FIFO，不能声明预测控制或多 Agent 已带来性能优势。

第一版[滚动对照回执](../evidence/rolling-v1/alibaba-gpu-series-2-rolling-ablation-v1.json)把同一预测/反馈能力放进完整外层决策闭环，在 5 个连续 holdout 日上比较普通 FIFO、固定预测、同预算规则、单 Agent、多 Agent 和非部署 oracle。每个滚动 arm 有 6 个 cutoff、每次 3 个候选，并在同一 simulator session 中连续执行；每个完整 arm/window 重复两次。FIFO 通过 5/5 窗口，固定预测、规则、单 Agent 和多 Agent 均为 1/5；后三者的聚合 allocation、Spot p95 JCT 与 eviction 完全相同。独立 Auditor 和 Manager 的[收口回执](../evidence/rolling-v1/alibaba-gpu-series-2-rolling-agentteams-closeout-v1.json)因此保留 `multi_agent_superiority_gate=not_established`，只把 FIFO 记为当前评估范围的安全回退，最终状态为 `approval_pending`。

新的[滚动 v2 回执](../evidence/rolling-v2/alibaba-gpu-series-2-rolling-ablation-v2.json)使用单独冻结的 2024-08-21 至 2024-08-25 窗口和新的 implementation fingerprint：预测需求按 cutoff 前可见的 HP Job 形状拆分，P90 校准只使用过去 validation residual，滚动安全动作保持 FIFO 排队与 placement、仅绕过当段过度保守的 Spot admission gate。30 条 arm/window 记录均有两次确定性重复。普通 FIFO、同预算规则、单 Agent、多 Agent 和非部署 oracle 都通过 4/5，平均 allocation 70.9875%、Spot p95 JCT 35,722.03s、eviction 0；固定预测通过 2/5，平均 allocation 70.0856%。多 Agent 因而优于固定预测，但与普通 FIFO 和两个同预算控制完全并列，两个 superiority gate 仍是 `not_established`。独立 Auditor 与 Manager 的[v2 收口回执](../evidence/rolling-v2/alibaba-gpu-series-2-rolling-agentteams-closeout-v2.json)进一步确认：没有任何可部署 arm 通过全部 5 个窗口，因此合格集合为空、推荐为 `null`，项目保留 `approval_pending` 且没有应用生产变更。

[滚动 v3 回执](../evidence/rolling-v3/alibaba-gpu-series-2-rolling-ablation-v3.json)把决策间隔缩短为 2 小时、加入双过去场景与 4 小时最小持有期，并增加同预算 Analyst 因果对照。5 个新 holdout 日、7 个 arm 共形成 35 条记录，每条均两次确定性复现；15 个 Agent controller 各连续完成 12 次隐藏未来决策，共验收 300 个 `deepseek-v4-flash` 阶段。普通 FIFO 通过 5/5；规则和遮蔽 Analyst 的多 Agent 为 4/5；单 Agent和完整多 Agent均为 3/5；固定预测为 2/5。完整多 Agent 与单 Agent 指标相同，并在匹配的 120 次模型调用、360 次候选仿真预算下劣于 4/5 的遮蔽 Analyst 对照，因此 `multi_agent_superiority_gate`、`multi_agent_vs_ordinary_gate` 和 `analyst_causal_value_gate` 全部是 `not_established`。独立 Auditor 与 Manager 的[v3 收口回执](../evidence/rolling-v3/alibaba-gpu-series-2-rolling-agentteams-closeout-v3.json)只推荐 `ordinary-fifo` 作为当前评估范围的安全回退，仍为 `approval_pending`，没有生产变更。由于非 Agent holdout 结果在提示词冻结前已对开发者可见，这个 full-vs-masked 结果明确标为探索性匹配对照，而不是完全盲化的确认性因果试验。

多窗口结果更接近真实结论：每个窗口先过 8 项硬 SLO，再按 allocation → Spot p95 JCT → eviction 分层决策，保留并列和无合格策略状态。12 窗口 v2 研究用于策略保护机制对比；当前 v3 进一步覆盖全部 112 个合格窗口，并采用 67/45 的时间顺序 calibration/holdout 切分。

在 45 个 holdout 窗口中，FIFO/校准集最佳固定策略只在 40 个窗口通过全部硬 SLO。AgentTeams 候选控制器在 41 个窗口找到合格策略，并以 185 个候选评估在 41/41 个可行窗口覆盖至少一个五动作正式分层最优动作；其待人工裁决前沿相对 FIFO 的平均 allocation uplift 为 +0.209～+0.257 个百分点。三候选 workload rule 使用 135 个评估并覆盖 39/41 个正式前沿，穷举目录则需要 225 个评估。候选搜索质量与仿真成本会同时报告，完整方法、限制与 fingerprint 见 [Adaptive Holdout Evaluation](../docs/adaptive-holdout-evaluation.md)。

单窗口演示可用 `scripts/run_demo_experiment.ps1` 一次执行。静态多窗口研究使用 `scripts/run_multiwindow_experiment.py`；预测研究使用可断点续跑的 `scripts/run_predictive_multiwindow_experiment.py`，并强制 calibration summary → selection lock → holdout 的执行顺序。已有 AgentTeams controller 时，`scripts/run_adaptive_demo.ps1` 可一次完成冻结设计、全 112 窗口实验、holdout 对照和公开回执。所有脚本都要求单独下载数据；预测 runner 对已完成任务逐项验 fingerprint 后恢复。

[返回项目首页](../README.md)
