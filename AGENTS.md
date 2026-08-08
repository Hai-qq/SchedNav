# SchedNav 工作约定

## 项目定位

项目比赛中文名为“智算领航：面向 GPU 集群的多智能体算力调度决策系统”，开源项目名为 `SchedNav`，英文定位为 “SchedNav — Agentic Control Plane for GPU Cluster Scheduling”。本项目在 GFS GPU simulator 之上构建高层 Agentic Control Plane；GFS 保留细粒度 Job-to-GPU/Node placement 权限。

## 当前边界

- V1 是 historical trace-driven counterfactual policy optimization，不宣称 online scheduling。
- 不重写 GFS，不让 LLM 直接选择 node、GPU 或逐 Job placement。
- 不引入 RL，不先做复杂前端。
- 性能结论必须来自同一真实 Trace window 的实际 simulation evidence。
- SLO、workload、metrics 和性能提升不得编造。

## 开发顺序

1. 跑通并固定 GFS baseline。
2. 封装 deterministic GFS adapter 和结构化 policy/metrics schema。
3. 实现 `analyze_workload`、`select_bounded_policies`、`simulate_policy`、`compare_policies`、`audit_slo`。
4. 最后把 Manager/Worker、任务拆解、状态和 human approval 映射到 AgentTeams。

## 上游与数据

- GitHub 公开仓库为 `https://github.com/Hai-qq/SchedNav`，`main` 是现役公开边界；后续 commit 或 push 仍需用户明确授权。
- `26ASPLOS-Spot/` 是固定的上游基座；修复应保持最小、可审计并优先放在 adapter/patch 层。
- `clusterdata/` 是 Alibaba 官方 sparse checkout；不要修改原始 CSV。
- SchedNav 第一方代码采用 MIT License；GFS / AgentTeams compatibility patch 和第三方许可证文本继续受对应上游许可证约束，不得用根目录 MIT 重新许可。
- GitHub 公开仓库只发布 SchedNav 自研代码、配置、测试和文档；不得提交 `26ASPLOS-Spot/`、`clusterdata/`、原始或派生 Trace、虚拟环境、checkpoint、日志、缓存、运行产物或凭据。
- 第三方基座与数据通过来源 URL、固定 commit/hash 和获取步骤复现，不复制到 SchedNav 仓库。
- 任何拟公开的小型 fixture 必须先核验来源、许可和脱敏边界。
- 实验必须记录 GFS commit、Trace commit/hash、window、warm-up、policy、seed、命令、退出码和 artifact hash。
- 每个候选策略必须使用独立进程和全新 Trace/Cluster 状态。

## 验证与文档

- canonical 配置包括 `configs/baselines/golden-a800-2024-04-07.json`、`configs/baselines/stress-gpu-series-2-2024-04-12.json`、FIFO 同窗配置与 `configs/action_spaces/v1-baseline.json` 中的 4 个精确 action profile；运行合同以 `docs/reproduction-contract.md` 和 `docs/policy-evaluation-contract.md` 为准。
- A800 golden gate、真实 eviction stress gate、FIFO/GFS 双运行与 4-policy portfolio gate 均已通过；正式 SLO 是 `configs/slos/schednav-demo-slo-v1.json`，指标与排名口径以 `docs/schednav-demo-slo-v1.md` 为准。不得把完整上游 profile delta 当作单变量因果提升，不得使用 LLM 自由加权分数或未声明的并列决胜规则。
- AgentTeams 固定 `v1.2.1`，采用 1 Manager + 4 standalone Worker；映射事实以 `docs/agentteams-integration.md` 为准。上游 `AgentTeams/`、构建 bundle 和 runtime 产物不得进入公开仓库。
- AgentTeams 的 LLM model ID 硬锁为 `deepseek-v4-flash`，Embedding 禁用；未经用户明确变更，不得调用、构建或配置其他模型。
- 先用真实小窗口做 golden/parity test，再跑全量窗口。
- 从 per-job/event artifact 统一计算指标，不盲信当前日志中的 `Preemption_rate` 标签。
- 代码、配置、metric schema 或运行方式变化时同步 `README.md` 和 `docs/` 中受影响的权威说明。
- 当前基线事实与阻塞项以 `docs/gfs-baseline-audit.md` 为准。
