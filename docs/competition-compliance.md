# SchedNav Agent Infra 合规映射

状态：依据 2026-08-07 公开赛道页核对。

## 必选与阶段要求

官方赛道要求至少 3 个不同职能 Agent，并以 AgentTeams（原 HiClaw）为协同设计基点，说明角色编排、任务拆解、上下文传递、协同执行与状态追踪。Skill 为必选项；高风险动作需要人工确认、审批、回滚和审计边界。若不使用 MCP，需要给出等价工具集成契约。若 V1 不使用 RAG，则应在 Agent 记忆、共享状态、轨迹可观测三项中至少实现两项。

初赛材料包括 500 字以内作品简介和方案 PPT；可执行 AgentTeams 代码包为可选。复赛要求可执行 AgentTeams 代码包和可运行 Demo 或 Demo 视频。赛程与提交物以[官方 Agent Infra 赛道页](https://goaihz.com/tracks?track=infra)后续通知为准。

## SchedNav 映射

| 赛道要求 | SchedNav V1 映射 | 当前状态 |
|---|---|---|
| 至少 3 个 Agent | Manager、Workload Analyst、Scheduling Strategist、Simulation Agent、SLO Auditor | Manager 与 4 个 standalone Worker 均已部署为 Running，模型全部锁定为 `deepseek-v4-flash` |
| 任务闭环 | Trace → workload → policy candidates → isolated simulation → metrics → SLO audit → recommendation | 四领域 finite project 已完整执行既有证据链；Manager compare/rank 得到三方并列，Admin 已人工接受 `repository-default-gfs` |
| 可复用 Skill | `analyze-gpu-workload`、`select-bounded-gfs-policies`、`simulate-gfs-policy`、`compare-gfs-policies`、`audit-gpu-slo` | 5 个 Skill 已建立并通过结构校验 |
| 结构化上下文 | WorkloadSummary、PolicyAction、RunManifest、MetricsReport、ComparisonReport、SLO Audit、Policy Ranking | 第一版 schema 已实现 |
| 结果验证 | 同窗 GFS counterfactual simulation；LLM 不自行判优 | FIFO/GFS 双运行、三类事件 ledger、4-policy portfolio gate 与全部候选 SLO 审计已通过 |
| 执行证据 | artifact hash、fingerprint、结构化 preemption/run/guarantee 事件、测试报告 | 第一版已实现 |
| Human approval | 高成本批量仿真启动前、最终策略接受前设 gate | YOLO 已关闭；首轮批准与最终接受的 event/time/decision maker 均已记录；最终选择明确标注为三方并列后的人工裁决，不宣称算法判优 |
| 共享状态 | AgentTeams room/task state 只传 artifact reference 与 schema 摘要 | 四个 finite task 已完整注册、流转和归档；完成后 `state.json.active_tasks` 为空，结构化结果保存在 MinIO |
| 轨迹可观测 | Skill 调用、仿真 manifest、metrics、audit 和 approval 事件 | 四 Worker 调用、Manager compare/rank、Matrix 消息、artifact refs/hash 和元数据纠错均已形成真实轨迹 |
| MCP 或等价契约 | V1 使用本地 subprocess adapter，并通过受限 host bridge 暴露给容器 Worker | 受限 MCP bridge 已验证委托认证、白名单、幂等、单执行通道、真实 Trace 调用和结构化 artifact 回读；无凭据 launcher 与五类 fail-closed 安全演示已完成 |

## 明确不堆叠的能力

V1 不引入 RAG、RL、在线调度或复杂前端。当前真实问题不依赖知识检索；共享状态和轨迹可观测更直接支撑评审所要求的结构化协作与可审计证据。MCP 只作为已有 GFS adapter 与 policy/metrics schema 的受限协议适配层，不重写底层调度器。
