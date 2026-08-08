# SchedNav 工作约定

## 项目定位

`SchedNav` 是面向 GPU 集群的 Agentic Control Plane。第一方 `schednav-sim` 负责具体队列推进、资源核算、抢占和 Job-to-Node/GPU placement；Agent 只负责负载分析、有限高层策略、仿真编排、SLO 审计和 human approval。

## 当前边界

- V1 是 historical trace-driven counterfactual policy optimization，不宣称 online scheduling。
- 不让 LLM 选择 job、node、GPU 或细粒度 placement。
- 不引入 RL，不先做复杂前端。
- 性能结论必须来自同一 Trace fingerprint、窗口、人口和执行控制下的实际 simulation evidence。
- SLO、workload、metrics、数据标签和性能提升不得编造。

## 开发顺序

1. 固定 canonical Trace Contract 与数据 provenance。
2. 实现并验证 deterministic first-party simulator、结构化 policy/metrics schema 和事件账本。
3. 完成 `analyze_workload`、`select_bounded_policies`、`simulate_policy`、`compare_policies`、`audit_slo`。
4. 把 Manager/Worker、任务拆解、状态和 human approval 映射到 AgentTeams。
5. 使用多个真实数据集做同数据集内的策略验证，不跨 Trace 直接比较绝对指标。

## 源码、许可与数据

- GitHub 公开仓库为 `https://github.com/Hai-qq/SchedNav`，`main` 是现役公开边界；后续 commit 或 push 仍需用户明确授权。
- `src/schednav/native_simulator.py`、canonical Trace Contract 与数据适配器是 SchedNav 第一方 MIT 代码。
- 不得把第三方代码改名、去归属后宣称为 SchedNav 自研；第一方实现和外部集成必须保持清晰许可边界。
- 原始数据、逐 Job 转换结果、虚拟环境、checkpoint、日志、缓存、运行产物和凭据不得进入公开仓库。
- Alibaba 与 Microsoft Philly 只是可选数据适配器；新数据集必须转换为 `schednav.trace/v1`，记录来源、版本、hash、过滤条件和语义映射。
- 数据源没有 HP/Spot 标签时不得自行推断。若使用外部映射，必须在 trace provenance 中明确记录，相关 SLO 限制同步公开。
- 任何拟公开的小型 fixture 必须明确为 synthetic contract fixture，不得冒充真实实验结果。
- 每个候选策略必须从同一初始 Trace 状态运行，记录 trace、policy、result 与 metrics fingerprint。

## 仿真与 Action Space

- 允许 Agent 控制的字段以 `configs/action_spaces/native-v1.json` 为准。
- placement strategy 固定为 `deterministic_best_fit`，不是 Agent action。
- 资源模型必须保留真实 fractional GPU demand，不能四舍五入为整卡。
- 抢占必须产生 Spot run、guarantee、rollback、overhead 和 preemption ledger。
- 指标与排名以正式 SLO 配置和 `src/schednav/metric_catalog.py` 为准；不得使用 LLM 自由加权分数或未声明的 tie-breaker。
- 当前内核限制以 `docs/native-simulator.md` 为准，不得把未实现的拓扑、网络、CPU、故障或在线预测能力写成已有功能。

## AgentTeams

- AgentTeams 固定 `v1.2.1`，采用 1 Manager + 4 standalone Worker；映射事实以 `docs/agentteams-integration.md` 为准。
- AgentTeams 的 LLM model ID 硬锁为 `deepseek-v4-flash`，Embedding 禁用；未经用户明确变更，不得调用、构建或配置其他模型。
- bridge 只暴露白名单结构化操作，不得开放任意 shell、路径或 placement 参数。
- Agent 间传递 artifact reference、schema、fingerprint、状态和小型摘要，不传原始 Trace 或大日志。

## 验证与文档

- canonical Trace 合同以 `docs/trace-contract.md` 为准，first-party simulator 以 `docs/native-simulator.md` 为准，数据集边界以 `docs/datasets.md` 为准。
- 真实小窗口先做 deterministic golden test，再扩大数据窗口；非 Alibaba 数据至少验证 ingestion、provenance 和 simulator compatibility。
- 修改代码、配置、metric schema、数据语义或运行方式时，同一增量同步 `README.md` 与受影响的 `docs/`。
- 每个可独立验收增量在最终答复前执行测试、公开边界检查和 `neat-freak` 收尾审计。
