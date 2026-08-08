# SchedNav × AgentTeams Integration

## 固定上游

SchedNav 固定 AgentTeams `v1.2.1`，commit `552d0fb54d697b0689dafb6a01740e1a5f507552`，Apache-2.0。上游仅作为本地部署依赖，不复制进 SchedNav 仓库。版本事实记录在 `third_party/manifest.json`。

AgentTeams 是容器编排与协作运行时，不替代领域 Agent 逻辑。它通过 Manager/Worker CR、Matrix Room、MinIO 共享文件、`state.json` 和 Human/Admin 可见性承担角色编排、任务分解、上下文传递、状态追踪与人工介入。

## 为什么使用 standalone Workers

V1 映射为一个 Manager 和四个 standalone Worker：

```text
Human Admin
    ↕ Matrix admin DM / Worker Rooms
SchedNav Manager
    ├─ Workload Analyst
    ├─ Scheduling Strategist
    ├─ Simulation Agent
    └─ SLO Auditor
```

AgentTeams 的 Team CR 会引入一个 Team Leader，并要求 Manager 只与 Leader 通信。SchedNav 的四个领域角色没有额外 Leader 职能；使用 standalone Worker 可保持“Manager 直接拆解给四个专职 Agent”的业务模型，也让 Global Admin 出现在每个 Worker Room 中。未来如果并行 Simulation Worker 数量扩大，再增加 Simulation Team，而不改变领域合同。

角色清单以 `configs/agentteams/agent-identities.json` 为准。AgentTeams CR 模板位于 `integrations/agentteams/schednav-resources.yaml.example`。

## 任务与上下文映射

每次 historical counterfactual evaluation 是 AgentTeams finite task/project。Manager 必须按 AgentTeams 官方 finite-task 约定创建 MinIO 目录、通知 Worker，并用官方 `manage-state.sh` 写入 `state.json`；不能手改状态文件。

```text
shared/tasks/{task-id}/
├─ meta.json                 # AgentTeams Manager 维护
├─ spec.md                   # 目标、窗口、验收条件
├─ schednav-state.json       # SchedNav 结构化阶段与 artifact refs
├─ base/                     # action space、SLO spec 等只读引用
├─ progress/                 # Worker 进度证据
└─ result.md                 # 人类可读摘要，不替代 JSON evidence
```

Agent 间只传 artifact path、schema version、fingerprint、状态与小型摘要。Alibaba Trace、GFS CSV、checkpoint 和大日志不进入 LLM 上下文，也不进入公开仓库。

任务阶段：

```text
received
  -> workload_analyzed
  -> policies_selected
  -> simulations_completed
  -> policies_compared
  -> slo_audited
  -> approval_pending
  -> approved | rejected
```

失败或不可用属于显式终态/分支，不允许 Manager 用自然语言补齐缺失 evidence。

## AgentTeams package

项目 Skills 位于 `.codex/skills/`，通过 `integrations/agentteams/package-manifest.json` 分配给角色。`scripts/build_agentteams_bundle.py` 以固定 ZIP 时间戳、排序成员和 SHA-256 receipt 构建五个 package，并把模型 ID 写入 CR 模板：

```powershell
python scripts\build_agentteams_bundle.py --model-id deepseek-v4-flash
```

输出进入被忽略的 `dist/agentteams/`，包含五个 ZIP、`schednav-resources.yaml` 与 bundle receipt。构建器拒绝覆盖已有输出目录，避免混入旧 package，并拒绝 `deepseek-v4-flash` 以外的 model ID。资源 apply 前必须人工检查模型、package path、网络和权限。

## Human approval

V1 禁止 AgentTeams YOLO mode。至少保留两个审批点：

1. 当候选数量、窗口或预计成本超过事先批准预算时，在启动 simulation batch 前由 Admin 确认；
2. 所有 simulation、comparison 和 SLO audit 完成后，Manager 只按 `configs/slos/schednav-demo-slo-v1.json` 声明的 allocation rate → Spot p95 JCT → eviction rate 层级生成推荐提案；不得自由加权，未决并列必须交给 Admin，最终提案也必须由 Admin 接受或拒绝。

当前正式 4-policy 结果已经触发该路径：FIFO 因 allocation rate 较低在排序阶段被排除，GFS 0.80/0.90/0.95 在三层指标上完全相同。Manager 的合法输出是三方并列提案与 `approval_pending`，不是任意指定一个 quantile。

审批事件必须记录 task ID、候选 action fingerprints、SLO fingerprint、证据 refs、决策人与时间。Simulator 已开始的本地运行不可“回滚”，只能安全终止并保留失败/取消 evidence；策略采用属于建议，不会直接修改真实 GPU 集群。

## 已验证 runtime

2026-08-08 已在 Windows 主机完成 AgentTeams `v1.2.1` embedded runtime 部署：

| 项目 | 已验证值 |
|---|---|
| Docker Desktop | Client / Server `29.6.2`，WSL 2 backend |
| PowerShell | `7.6.4` |
| AgentTeams runtime | embedded Controller + CoPaw Manager + 4 个 CoPaw standalone Worker |
| LLM provider | `openai-compat` |
| Base URL | `https://api.deepseek.com` |
| 默认模型 | `deepseek-v4-flash` |
| Embedding | 禁用；不调用其他模型 |
| 网络暴露 | 仅 `127.0.0.1` |
| 端口 | Gateway `18080`、Higress Console `18001`、Element Web `18088`、Manager Console `18888` |
| 运行状态 | Controller、Manager 与 4 个领域 Worker 均为 Running |
| Manager 状态 | `name=default`、`phase=Running`、`runtime=copaw`、`welcomeSent=true` |
| 模型验证 | 通过 AgentTeams/Higress Gateway 调用 `/v1/chat/completions`，响应模型为 `deepseek-v4-flash` |

凭据只保存在用户目录的 machine-local `agentteams-manager.env`，不得复制到项目、日志、任务 artifact 或聊天上下文。V1 保持 YOLO mode 禁用。

固定版本的 Windows 安装器缺少同版本 shell 安装器已有的 Matrix AppService 凭据逻辑；不修复时 embedded Controller 无法创建 Manager。最小 parity patch 位于 `patches/agentteams/windows-appservice-parity.patch`，只补充凭据生成、env 持久化和 Controller 透传，不修改 AgentTeams 的协作模型、Agent 实现或调度逻辑。上游 `AgentTeams/` 工作树和容器镜像仍是本地依赖，不进入公开仓库。

## Tool boundary 与已验证 host bridge

SchedNav deterministic Python CLI 运行在 Windows 主机，AgentTeams Worker 运行在 Linux 容器。`src/schednav/host_bridge.py` 通过 `/mcp` 把两者连接起来，只暴露以下固定操作：

- 异步任务：`analyze_workload`、`simulate_policy`、`compare_policies`、`audit_slo`、`rank_policies`；
- 结构化读取：`get_task`、`read_artifact`。

bridge 使用 `configs/agentteams/host-bridge-v1.json` 固定 project/artifact root、Trace manifest、run config、action profile、metrics 和 SLO catalog；请求字段、schema、profile 与 artifact 路径全部做白名单校验。它使用持久化哈希幂等回执、单执行通道、64 KiB 请求上限和 512 KiB 结构化 JSON 读取上限，不读取原始 CSV/log/failure artifact，也不暴露任意 shell。容器身份由 AgentTeams Gateway 的 `/v1/models` 委托校验；内存缓存只保存 token SHA-256，不把凭据写入 API 状态或任务日志。有效 Worker/Manager 身份已验证为 `200`，无效凭据已验证为 `401`。

真实链路已验证：Workload Analyst 通过 MCP 提交 Alibaba Trace 分析任务 `8aa6db1a57c543179b89b6c5c2642b08`，Windows host 生成 `agentteams-bridge/tasks/8aa6db1a57c543179b89b6c5c2642b08/workload-summary.json`；Manager 随后通过 `read_artifact` 校验了 `schednav.workload-summary/v1` 和 SHA-256。该结果证明跨容器协议、委托认证、真实 Trace 调用和结构化回读均可用，不把大 CSV 塞入 Agent 上下文。

四个 Worker 的 package 与 Skill 已按各自 scoped identity 部署。AgentTeams v1.2.1 embedded Manager 不会把自定义 package Skill 和 `mcpServers` 自动投影到 CoPaw，因此 Manager Skill 使用官方 host workspace，MCP 客户端配置保存在 Manager machine-local config 中并限制为 `0600`；这些运行态文件不进入公开仓库。当前 bridge 是本次开发会话中的前台进程，还没有安装为 Windows 自启动服务，主机或会话重启后需要运行：

```powershell
.\scripts\start_host_bridge.ps1
```

脚本固定使用项目 `.venv-gfs`、catalog config 和 AgentTeams Gateway 委托认证；检测到 direct-token 环境变量或端口被非 SchedNav 服务占用时会拒绝启动。`-CheckOnly` 只验证当前服务。`scripts/test_host_bridge_safety.ps1` 会运行定向单元测试和 live probe，验证越界字段、未登记 action、未知 SLO、缺失/无效 bearer 与不可用 bridge 全部 fail closed；它不需要、读取或输出有效凭据。

## 已完成 finite project 与最终 Gate

Manager 已创建 finite project `proj-20260808-060606`，并拆成四个领域任务：

- Workload Analyst：`task-20260808-060700`；
- Scheduling Strategist：`task-20260808-060701`；
- Simulation Agent：`task-20260808-060702`；
- SLO Auditor：`task-20260808-060703`。

本轮只复用已有四策略真实证据，没有启动新 GFS simulation。YOLO mode 明确关闭；Admin 通过 Matrix event `$qGC8qC-_eZluoK32RYY729icwiQWt5TBr7HztokQ3AQ` 在 `2026-08-08T07:48:34.754Z` 批准计划后，Manager 才开始派发。实际执行结果：

1. Workload Analyst 生成并回读真实 WorkloadSummary；
2. Scheduling Strategist 恰好选择四个白名单 profile，Simulation Agent 校验四份现有 MetricsReport；
3. SLO Auditor 对四个候选执行同一 `schednav-demo-slo-v1`，全部通过 8/8 硬约束，每个保留 allocation `<80%` 的软目标失败；
4. Manager 调用 `compare_policies` 与 `rank_policies`，产生 `tie_requires_human_approval`：FIFO 在 allocation 阶段被排后，GFS 0.80/0.90/0.95 在 allocation、Spot p95 JCT 和 eviction rate 三层上完全并列；
5. 四个领域 task 均已归档，官方 `state.json` 的 `active_tasks` 为空；Manager 先把项目停在 `approval_pending`，没有自动采用策略。

Phase 4 的 bridge artifacts 为：

- portfolio：`agentteams-bridge/tasks/45371227fbac48c190fe0bc54756eab5/policy-portfolio.json`，SHA-256 `c33142fd32b1bcd876eb72cbf18be052d4f183f0ea199dce7c55c99bf4cfdc24`；
- ranking：`agentteams-bridge/tasks/b5197ef5b139444892fd40e6b5c394ac/policy-ranking.json`，SHA-256 `4ce521ae5046bcdd5425cf8c5e1be8d0adeabeaab3346b9dbd67507d955561b4`，ranking fingerprint `8752f40df9c9ef5edbc31be189bd6804c30ba4fc5d9eaf66488ba0c85fd083b3`。

最终提案最初误把 portfolio hash 写成 ranking hash，并把首轮批准时间写为 `06:10Z`；Admin 审计发现后，Manager 已核对原始 artifact 与 Matrix event，只修正元数据并重新同步 MinIO，没有修改 ranking、SLO 或候选结论。第二道审批中，Admin 通过 Matrix event `$tsoSdbrIFMyNwaDnGSY7rGeRjjf1PLMpzb-FOwJztMk` 在 `2026-08-08T08:08:04.556Z` 接受 `repository-default-gfs`，policy fingerprint 为 `a7fe2f906bf44eddfbf40ca4c9c9284bdcd09663d3ebeb5dafee75f05ed425d2`。Manager 已将项目 outcome 收敛为 `accepted`，并在 `accepted_policy.selection_basis` 中明确记录：这是三方证据并列后的人工选择，不是 0.90 优于 0.80/0.95 的性能结论。

后续仍需另行完成：

1. 将已验证的一键前台 launcher 封装为可恢复的 Windows 自启动服务；
2. 如需打破三方并列，创建新的、单独批准的 simulation project，并保留本轮 accepted 证据不变。
