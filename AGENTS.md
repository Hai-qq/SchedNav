# SchedNav 工作约定

## 项目定位

`SchedNav` 是面向 GPU 集群的 Agentic Control Plane。第一方 `schednav-sim` 负责具体队列推进、资源核算、抢占和 Job-to-Node/GPU placement；Agent 只负责负载分析、有限高层策略、仿真编排、SLO 审计和 human approval。

## 当前边界

- V1 实验边界是 historical trace-driven counterfactual policy optimization。
- `schednav.predictive-controller/v1` 提供轻量 aggregate baseline；`schednav.tenant-predictive-controller/v1` 已实现按租户/资源池的一分钟观测、28 天概率模型、每日重训、P90 Spot quota、保障周期反馈与 cutoff-safe replay。外层 rolling control 已实现有界策略选择、同一 simulator session 内连续推进、精确状态交接、隐藏未来到达、SLO 审计和 Manager human-approval 收口。它仍不等于已接入真实集群，不得宣称已有生产在线调度或性能优势。
- 不让 LLM 选择 job、node、GPU 或细粒度 placement。
- 不引入 RL，不先做复杂前端。
- 性能结论必须来自同一 Trace fingerprint、窗口、人口和执行控制下的实际 simulation evidence。
- SLO、workload、metrics、数据标签和性能提升不得编造。

## 开发顺序

1. 固定 canonical Trace Contract 与数据 provenance。
2. 实现并验证 deterministic first-party simulator、结构化 policy/metrics schema 和事件账本。
3. 完成 `analyze_workload`、`select_bounded_policies`、`simulate_policy`、`compare_policies`、`audit_slo`。
4. 把 Manager/Worker、任务拆解、状态和 human approval 映射到 AgentTeams。
5. 保持预测控制内环和带状态交接的外层 rolling policy decision 均为 cutoff-safe；任何新实验都必须隐藏未来 Trace。
6. 使用多个真实数据集做同数据集内的策略验证，不跨 Trace 直接比较绝对指标。

## 源码、许可与数据

- GitHub 公开仓库为 `https://github.com/Hai-qq/SchedNav`，`main` 是现役公开边界；后续 commit 或 push 仍需用户明确授权。
- `src/schednav/native_simulator.py`、canonical Trace Contract 与数据适配器是 SchedNav 第一方 MIT 代码。
- 不得把第三方代码改名、去归属后宣称为 SchedNav 自研；第一方实现和外部集成必须保持清晰许可边界。
- 原始数据、逐 Job 转换结果、虚拟环境、checkpoint、日志、缓存、运行产物和凭据不得进入公开仓库。
- Alibaba 与 Microsoft Philly 只是可选数据适配器；新数据集必须转换为 `schednav.trace/v1`，需要租户预测时转换为带来源映射的 `schednav.trace/v2`，并记录来源、版本、hash、过滤条件和语义映射。
- 数据源没有 HP/Spot 标签时不得自行推断。若使用外部映射，必须在 trace provenance 中明确记录，相关 SLO 限制同步公开。
- 任何拟公开的小型 fixture 必须明确为 synthetic contract fixture，不得冒充真实实验结果。
- 每个候选策略必须从同一初始 Trace 状态运行，记录 trace、policy、result 与 metrics fingerprint。

## 仿真与 Action Space

- 允许 Agent 控制的字段以被实验或任务显式引用的 `configs/action_spaces/*.json` 为准；单窗口默认使用 `native-v1.json`，当前全窗口自适应研究使用 `native-multiwindow-v3.json`，v1/v2 action space 仅用于复现实验历史。
- placement strategy 固定为 `deterministic_best_fit`，不是 Agent action。
- 每个 rolling Action Space 必须显式声明 `safety_baseline_action_id`；Agent 候选必须包含 `pending_observation.required_candidate_action_id`，不得跨版本复用或把 `native-fifo` 写死。当前 v2/v3 安全动作是保持 FIFO 排队/placement、仅绕过该时段预测 admission gate 的 `rolling-fifo-open`；v3 还要求变更动作至少保持四小时。
- `preemption_victim_strategy` 只允许目录声明的确定性高层规则，具体 victim 仍由 simulator 选择。
- 资源模型必须保留真实 fractional GPU demand，不能四舍五入为整卡。
- 抢占必须产生 Spot run、guarantee、rollback、overhead 和 preemption ledger。
- 预测控制器只能接收当前 scheduler state 和截至 cutoff 的历史；未来目标只能在到达后用于 forecast scoring。轻量与 tenant-aware 控制合同分别以 `configs/controllers/predictive-spot-v1.json`、`configs/controllers/tenant-predictive-spot-v1.json` 和 `docs/predictive-control.md` 为准。tenant-aware 运行必须使用 trace/v2、非空 tenant ID 和具体资源池。
- 当前真实 tenant-aware 单窗证据通过 7/8 项硬 SLO，但 allocation 低于同 Trace FIFO，因此只能声明实现链路可执行和确定性，不得声明性能优势。
- 当前 11 窗口预测证据按时间切为 6 个 calibration 与 5 个 holdout，并在任何 holdout 运行前写入 selection lock。FIFO/guarded-static 在 holdout 均通过 5/5，tenant-predictive 通过 1/5，aggregate-predictive 通过 0/5；两个预测 arm 均因 allocation 非退化约束失败。冻结证据已经只读复核，Agent 必须保留 `approval_pending / no_calibration_eligible_arm / selected=[]`，不得声明预测控制或多 Agent 已证明性能优势。
- 外层 rolling v1 对照在 5 个 holdout 窗口比较普通 FIFO、固定预测、同预算规则、单 Agent、多 Agent 和仅供分析的 posthoc oracle；FIFO 与 oracle 通过 5/5，其他四个 arm 均为 1/5，规则、单 Agent 和多 Agent 聚合结果并列。该证据绑定 `schednav.past-replay-scenario/v1`，只用于对应 implementation fingerprint。
- rolling v2 已用新的 implementation fingerprint 和单独冻结的 2024-08-21 至 2024-08-25 holdout 完成 30 条记录。候选 evaluator 将聚合预测按 cutoff 前可见的 HP Job 形状拆分，tenant controller v2 只用 cutoff 以前的 validation residual 校准 P90。普通 FIFO、同预算规则、单 Agent、多 Agent 和 posthoc oracle 均通过 4/5 且聚合指标完全并列；固定预测通过 2/5。AgentTeams 项目 `proj-20260811-042605` 已完成终端聚合、独立 Auditor 与 Manager 收口，最终 `eligible=[] / recommended=null / completed / approval_pending`，没有生产变更。正式 gate 仍为 `multi_agent_superiority_gate=not_established` 和 `multi_agent_vs_ordinary_gate=not_established`。两代记录不得混合，不得自动部署或宣称多 Agent 性能优势。
- rolling v3 以 `schednav.past-replay-scenario/v3` 修正预测与 HP carry-over 重复计数、继承队列等待年龄，并用 calibrated-P90 + recent-history stress 双情景评估。5 个窗口、每两小时决策、四小时最短动作保持的正式研究已完成 35 条双重复记录和 300 个验签 Agent 阶段：普通 FIFO 通过 5/5，规则与 fixed-mask 多 Agent 通过 4/5，单 Agent 与 full-handoff 多 Agent 通过 3/5，固定预测通过 2/5。full-handoff 在匹配预算下劣于 fixed-mask，三个优越性/因果价值 gate 均为 `not_established`；Manager 只推荐 `ordinary-fifo` 作为评估范围内回退，保持 `approval_pending`，没有生产变更。因 amendment 前已看过非 Agent holdout 结果，只能报告透明的 exploratory matched-handoff evidence，不能写成完全盲化因果证明。
- 指标与排名以正式 SLO 配置和 `src/schednav/metric_catalog.py` 为准；不得使用 LLM 自由加权分数或未声明的 tie-breaker。
- 当前内核限制以 `docs/native-simulator.md`、`docs/predictive-control.md` 与 `docs/rolling-control.md` 为准；不得把未实现的拓扑、网络、CPU、故障、live-cluster adapter、持久恢复或 actuator rollback 写成已有功能。

## AgentTeams

- AgentTeams 集成契约以 `v1.2.1` 源码检出为基准，采用 1 Manager + 4 standalone Worker；不得据此假定同名运行镜像标签存在，实际部署必须另行记录镜像引用或 digest。映射事实以 `docs/agentteams-integration.md` 为准。
- AgentTeams 的 LLM model ID 硬锁为 `deepseek-v4-flash`，Embedding 禁用；未经用户明确变更，不得调用、构建或配置其他模型。
- bridge 只暴露白名单结构化操作，不得开放任意 shell、路径或 placement 参数。
- Agent 间传递 artifact reference、schema、fingerprint、状态和小型摘要，不传原始 Trace 或大日志。

## 验证与文档

- canonical Trace 合同以 `docs/trace-contract.md` 为准，first-party simulator 以 `docs/native-simulator.md` 为准，预测控制以 `docs/predictive-control.md` 为准，数据集边界以 `docs/datasets.md` 为准，多窗口方法与结论以 `docs/multiwindow-evaluation.md` 及其公开回执为准。
- 真实小窗口先做 deterministic golden test，再扩大数据窗口；非 Alibaba 数据至少验证 ingestion、provenance 和 simulator compatibility。
- 修改代码、配置、metric schema、数据语义或运行方式时，同一增量同步 `README.md` 与受影响的 `docs/`。
- 每个可独立验收增量在最终答复前执行测试、公开边界检查和 `neat-freak` 收尾审计。
