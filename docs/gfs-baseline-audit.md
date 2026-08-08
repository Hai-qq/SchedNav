# SchedNav：GFS baseline 与 Agentic Control Plane V1 审计

审计更新日期：2026-08-08（Asia/Shanghai）

项目比赛中文名为“智算领航：面向 GPU 集群的多智能体算力调度决策系统”，开源项目名为 `SchedNav`，英文定位为 “SchedNav — Agentic Control Plane for GPU Cluster Scheduling”。项目参加世界人工智能开源大赛 [Agent Infra 新智基座赛道](https://goaihz.com/tracks?track=infra)。GFS 基座、Alibaba Trace、虚拟环境和运行产物均为本地依赖或实验输入，不进入 SchedNav 公开仓库。

## 1. 已固定的上游输入

- GFS：`MachineLearningSystem/26ASPLOS-Spot`
  - commit：`e998d5453e626a0b743b3fd5137c54c987db780b`
  - 本地目录：`../26ASPLOS-Spot`
- Alibaba Trace：`alibaba/clusterdata/cluster-trace-v2026-spot-gpu`
  - clusterdata commit：`0d0f3f1efdbf1add6a7bcc63676eafbd1eb11f71`
  - 使用 sparse checkout，只取 `cluster-trace-v2026-spot-gpu`
  - 本地目录：`../clusterdata/cluster-trace-v2026-spot-gpu`

Trace 已核验：

- 4,278 个节点，10,412 张 GPU，6 种 GPU 型号；
- `job_info_df.csv` 有 466,867 条作业记录；
- 按当前 `utils.trace_process` 的固定时间范围和过滤规则，模拟器实际装载 428,535 个作业：415,691 HP、12,844 Spot；
- 当前评估窗口（2024-08-01 至 2024-08-31）有 41,538 个 HP 和 12,844 个 Spot 作业。

## 2. 当前 GFS 架构

```text
node_info_df.csv -> Cluster -> VC(by gpu_model) -> Node/GPU state
job_info_df.csv  -> trace_process -> Trace -> Job
                                      |
                                      v
                           Policy simulation loop
                         /                        \
              GPURequestEstimator             Placer
                 OrgLinear              non-preemptive/preemptive
                         \                        /
                          Spot quota + scheduling
                                      |
                                      v
                                  Recorder
       per-job / preemption-run-guarantee events / cluster-seq / VC-seq CSV
```

关键事实：

- `Cluster` 按 `gpu_model` 把节点分成 6 个互不迁移的 VC；没有跨 GPU 型号放置。
- `Job` 是可变字典；状态主线为 `None -> pend -> run -> end`。Spot 被抢占后从 `run` 回到 `pend`。
- `worker_num` 由 placer 作为 gang-like 副本数处理：每个 worker 都要完成一次 `gpu_request` 放置。
- `SpotScheduler` 对 HP 先尝试非抢占放置；评估窗口内失败后，才允许 HP 抢占 Spot。Spot 始终只做非抢占放置。
- GFS 的预测链路是 OrgLinear：按 organization 记录 HP GPU demand，使用 672 小时历史、4 小时预测（当前默认），输出均值和方差；SQA 用目标分位数估算未来空闲 GPU，再生成 Spot quota。
- recorder 每 60 秒采样集群状态，结束后输出作业表、集群序列表、VC 序列表和日志摘要。

当前仓库包含 5 个 policy 实现：

- `fifo_spot`
- `spot_scheduler`（GFS）
- `chronus`
- `lyra`
- `FGD`

但 CLI 和 `--sweep` 只暴露 `fifo_spot` 与 `spot_scheduler`。

## 3. 当前源码真正暴露的控制变量

### 3.1 可由 CLI 直接设置

| 类别 | 变量 | 当前作用 |
|---|---|---|
| 实验 | `experiment_name`, `trace_dir`, `log_dir` | 输入输出路径与实验名 |
| scheduler | `scheduler` | 仅允许 `fifo_spot`、`spot_scheduler` |
| scheduler | `sweep` | 顺序运行上述两个 scheduler；当前实现不能作为可信对比，见阻塞项 |
| Spot SLO | `guarantee_hour` | quota 的保障时长键；上游默认/CLI 类型不一致，SchedNav patch 统一为整数列表 |
| Spot SLO | `guarantee_rate` | 预测分布分位数目标，默认 0.9 |
| checkpoint | `ckpt_interval` | Spot 抢占后的 checkpoint rollback，默认 3,600 秒 |
| forecast | `seq_len`, `pred_len`, `freq`, `scaling` | 预测历史长度、预测范围、频率、缩放 |
| estimator | model/optimizer/GPU 参数 | OrgLinear 结构、训练和设备参数 |
| placement | `colocate` | 当前未被后续代码读取，是 no-op，不能视为有效 action |

上游 `trace_range` 与 `log_range` 写死在 `simulator.py`；SchedNav compatibility patch 已通过 `--trace-start/--trace-end/--log-start/--log-end` 外部化，但它们属于固定实验合同，不属于 Agent policy action。

### 3.2 源码中存在但未外部化的策略旋钮

- HP/Spot 队列排序键；
- non-preemptive placement 的 best-fit、同类型聚合和 eviction-aware 排序；
- eviction score 的短/长窗口权重 `hour_weight=0.8` 与惩罚幂 `penalty_power=2`；
- preemption cost 中 eviction 部分与 wasted GPU-time 的权重（源码硬编码为 `5.0`）；
- 动态 quota 的 `eta` 调整阈值（目标违约率的 0.5x/1.5x）和 3,600 秒排队阈值；
- quota 更新周期 300 秒、预测器训练周期 1 天、recorder 周期 60 秒；
- Spot 的单次保障周期 3,600 秒；
- 40/60 秒的抢占 overhead；
- VC 节点增减接口 `update_vc_node`，但没有被当前 policy 使用。

这些是候选 action，不等于现在已经安全可控。V1 必须先通过 adapter 显式建模、校验范围并做 parity test。

## 4. 当前 metrics

### 4.1 simulator 自动生成

Per-job：

- `submit_time`, `start_time`, `end_time`, `jct`, `queue`, `status`；
- `preempt_times`, `remain`, `nodes`；
- 作业类型、GPU 型号、organization、`gpu_request`, `worker_num`, `duration` 等原始字段。

Preemption event ledger（SchedNav compatibility patch 增加，只读观察）：

- event time/cause、preemptor/preempted job identity；
- rollback、overhead、remain before/after；
- requested GPUs 与新增 requested-GPU-seconds；
- event 是否位于 evaluation window、是否计入 Spot failure。

Spot run-start / guarantee event ledger（SchedNav compatibility patch 增加，只读观察）：

- 每次初次启动或抢占后恢复启动的作业、时间和 per-job run ordinal；
- 每次保障事件的 succeeded/failed outcome、reason 和保障周期内运行时长；
- 两类事件是否发生在 evaluation window；正式指标按 evaluation arrival population 过滤并包含 drain 事件。

Cluster/VC time series：

- total / idle GPU；
- HP / Spot 已分配请求量；
- HP / Spot 排队 GPU 量和排队作业数；
- running job 数；
- total / consolidate / shared node 数；
- `gpu_utilization`：实际是已分配卡比例（allocation rate），不是 GPU core utilization。

日志摘要：

- HP、Spot、overall、per-VC 的平均 JCT 与平均 queue；
- 平均 `preempt_times`（日志把它命名为 `Preemption_rate`）；
- 平均 GPU allocation rate；
- useful GPU-time rate；
- Spot guarantee-hour 的 succeed / failed 和相应比例。

### 4.2 estimator 库中存在但 simulator 未自动汇总

- MAE、MSE、RMSE、MAPE、MSPE；
- 0.9/0.95 quantile recall。

### 4.3 V1 不能直接照搬的口径

- `preempt_times` 在不同 scheduler 中更新不一致；例如 Chronus 的 lease preemption 不增加该字段。V1 只承诺已验证的 `fifo_spot` / `spot_scheduler` 路径。
- `ckpt_times` 虽然出现在 per-job CSV，但源码只初始化为 0、从未递增，不能作为 checkpoint count。
- 日志中的 Spot `Preemption_rate` 是 guarantee-hour 事件比例，不应直接当作论文定义的“evictions / runs”。
- queue demand 和 GFS 的 `gpu_request_dict` 多处未乘 `worker_num`，会低估 gang 作业的总需求。
- allocation rate 把部分卡分配视为整卡已分配；它不是硬件利用率。

因此 V1 应从 per-job 与事件数据重新计算统一指标，并把公式、分母、时间窗口写入结果 schema。

## 5. 官方 baseline 复现状态

运行环境：Python 3.11 独立 venv，按上游 `requirements.txt` 安装。

上游运行依赖缺口：

- 源码导入 `chinese_calendar`，但 `requirements.txt` 未声明；手动安装 `chinese-calendar==1.11.0` 后入口可启动。

执行的官方默认命令等价于：

```powershell
python simulator.py `
  --experiment-name official-default-spot_scheduler `
  --trace-dir ..\clusterdata\cluster-trace-v2026-spot-gpu `
  --log-dir ..\artifacts\gfs-baseline `
  --scheduler spot_scheduler
```

实测进度：

- 装载 428,535 个作业；
- 模拟时间 0：3,588 running；
- 第 1 个模拟日到达约需 126 秒，A10 HP pending 为 2,153；
- 第 2 个模拟日又需约 215 秒，A10 HP pending 增至 6,243；
- 主循环每个模拟秒对 pending queue 重新排序并遍历，运行时间会随积压显著增长。

该进程在第 2 个模拟日后人工停止。没有产生可声明为 baseline 的最终 metrics。

即使继续运行到固定评估窗口起点（第 153 个模拟日），预测器首次训练也会确定失败：`gpu_request_estimator.data_provider` 读取 `args.embed`，而 CLI 从未定义该参数。最小调用已复现：

```text
AttributeError: 'types.SimpleNamespace' object has no attribute 'embed'
```

### 5.1 已完成的真实 Trace smoke run（不是 GFS baseline）

为验证非预测路径，选取 2024-08-01 的真实 A100 Trace 行：25 HP + 25 Spot，限制 `duration <= 3600`、`worker_num <= 4`，调用源码内置但未暴露到 CLI 的 Chronus。

结果：

- 50/50 作业进入 `end`；模拟结束时间 42,300 秒；
- 生成 `chronus_log.csv`、`chronus_seq.csv`、`chronus_vc_record.csv`；
- HP mean JCT 2,127.92 秒，p95 JCT 3,572.4 秒，mean queue 1,551.68 秒；
- Spot mean JCT 1,480.12 秒，p95 JCT 3,037.0 秒，mean queue 0 秒。

这些数字只用于证明 lifecycle/recorder 链路，不能用于 scheduler 优劣结论。

### 5.2 已通过的 deterministic golden gate（不是完整 baseline）

在第一方 adapter 中固定 GFS/Trace commit、compatibility patch hash、A800 VC、真实 Trace window、CPU、单进程 DataLoader 和 seed。窗口使用 2024-03-01 至 2024-04-07 作为预测 warm-up，evaluation 为 2024-04-07 00:00:00 至 11:59:59，并 drain 到已纳入作业全部结束。

TraceManifest：

- 22 个 A800 节点、176 张 GPU；
- 1,286 个 HP 与 evaluation 内 27 个 Spot；
- 原始与派生 CSV 均记录 SHA-256，派生数据只存在于被忽略的 `artifacts/`。

两个独立进程 `r3-attested`、`r4-attested` 均重新训练 estimator 并完成 1,313 个作业。Job、cluster sequence、Spot quota、VC sequence 四个 CSV 的逐文件 SHA-256 完全一致，comparison 为 `deterministic_match=true`。

evaluation arrival population 的 canonical MetricsReport：

| 指标 | HP | Spot |
|---|---:|---:|
| job / completed | 5 / 5 | 27 / 27 |
| JCT mean | 10,969.6s | 1,174.444444s |
| JCT p50 | 3,877s | 1,089s |
| JCT p95 | 35,400.4s | 1,664.7s |
| queue mean / p95 | 0 / 0 | 0 / 0 |
| preemption count | 0 | 0 |

720 个一分钟样本上的 mean GPU allocation rate 为 0.222185。该窗口验证了真实 HP/Spot 输入、OrgLinear 训练、quota 更新、调度、drain、recorder 和确定性证据链，但没有发生抢占，所以不能验证 eviction、rollback 或 guarantee failure，也不能作为 scheduler 性能提升结论。

### 5.3 已通过的真实 eviction stress gate

确定性 Trace scanner 在 2024-04-06 至 2024-08-31 的 GPU 型号/自然日组合上排名候选。`GPU-series-2 / 2024-04-12` 排名第 3，且是前三名中日期最早的候选。该评分只决定仿真顺序，不作为抢占事实。

固定窗口包含 122 个节点、976 张 GPU、5,792 个 warm-up/evaluation HP 和 evaluation 内 84 个 Spot。最终 SLO 账本版 repository-default GFS 的两个隔离进程均完成全部作业，七个 GFS CSV 的 SHA-256 完全相同，comparison 为 `deterministic_match=true`。

evaluation arrival population 中有 94 HP 与 84 Spot，均全部完成；1 个 Spot 作业累计发生 2 次抢占，两个 ledger-aware eviction gate 均通过。Spot preempted-job rate `1/84 = 0.0119047619` 仅作作业级参考；正式口径为 86 次 run 中 2 次 eviction，eviction rate `0.023255813953488372`。403 个保障事件中 401 成功，guarantee success rate `0.9950372208436724`。1,440 个一分钟样本的 mean allocation rate 为 0.733534。事件账本还记录 3,304 秒 rollback、80 秒 overhead 和 226,728 requested-GPU-seconds 新增工作量。checkpoint count 仍不可观测。详见 `docs/eviction-stress-gate.md`。

### 5.4 已通过的同窗 policy portfolio gate

同一 stress window 上，repository-default FIFO 与 GFS 均完成两次隔离运行并逐文件确定；GFS guarantee quantile 0.80 与 0.95 各完成一次真实 evidence run。4-candidate portfolio 的源码、Trace、窗口、population、固定执行控制和 event consistency 全部一致，`comparable=true`。

从 FIFO 到 default GFS 的实测 `right - left` delta：HP mean JCT `-39.148936s`，Spot mean JCT `-20,980.25s`，Spot mean queue `-21,020.535714s`，allocation rate `+0.64331`，同时 Spot preemption count `+2`、新增 requested-GPU-seconds `+226,728`。上游两种 profile 不只改变一个启发式，这些 delta 不能解释为单变量因果提升。

GFS 0.80/0.90/0.95 的 quota CSV 各不相同，但最终账本版 job、三类 event、cluster sequence、VC record 和 canonical 排名指标完全相同。这是当前窗口中的“无可观测指标差异”，不是预设结论。四个候选均通过 `SchedNav Demo SLO v1` 的全部硬约束；声明的 allocation rate → Spot p95 JCT → eviction rate 层级先排除 FIFO，三个 GFS profile 仍并列，状态为 `tie_requires_human_approval`。系统没有使用加权综合分数或未声明的第四级规则。

## 6. 复现前必须处理的源码风险

按优先级：

1. 第一方 compatibility patch 已补齐缺失依赖、`args.embed`、list 参数、可配置 evaluation/trace end、seed/CPU deterministic profile 和 Windows checkpoint 路径；完整 baseline 仍须验证这些补丁在全量窗口上的行为。
2. 解决全量模拟的 per-second queue sort/scan；必须做等价性测试，不能直接重写成未经验证的新 simulator。
3. 每个候选策略使用独立进程和全新 `Trace`/`Cluster`。当前 `--sweep` 复用可变 Job 和 Cluster，第二个策略不是同一初始状态。
4. 修复或明确 `worker_num` 的计量口径：placer 按 `gpu_request * worker_num` 分配，但 GFS quota/recorder 多处只累计 `gpu_request`。
5. 上游 eviction history 链路仍有独立缺陷：抢占逻辑先清空 `job["nodes"]` 再记录旧 history，`get_eviction_rate` 构造 DataFrame 时也丢弃 `succeed` 列；V1 ledger 不依赖这条 history。
6. golden adapter 已固定 estimator seed、CPU、线程、独立 checkpoint 和 `num_workers=0`；后续候选评估必须沿用该隔离合同。
7. 结构化 preemption、Spot run-start 与 Spot guarantee event ledger 已增加并通过双运行 parity；正式 eviction/guarantee 指标已统一，checkpoint count 仍不可观测，且自然语言日志标签不作为 SLO 事实。
8. `colocate`、CPU request、动态节点接口目前不能进入 action space：前者是 no-op，CPU 未参与调度，后者未接入 policy。

论文参数与当前代码默认值也不完全一致：论文列出 guarantee hours `[1, 2, 4]`、penalty `m=3`，当前源码默认分别为 `[1]`、`penalty_power=2`。复现实验必须明确选择“paper profile”还是“repository default profile”，不能混称。

## 7. 建议的 V1 最小实现

开发顺序保持为：

```text
GFS reproduction gate
  -> deterministic GFS adapter
  -> policy evaluation CLI
  -> structured skills
  -> AgentTeams orchestration
```

### 7.1 第一增量：Reproduction Gate（golden、eviction stress 与同窗 portfolio 已通过）

- 不改 scheduler 算法，只修运行契约和可配置窗口；
- 已建立真实 A800 golden trace window；
- 已验证同一配置两个隔离进程得到完全相同的 evidence 文件集合；
- 已验证一个真实窗口可重复触发 Spot 抢占，并由结构化 eviction gate 审核；
- 已验证 FIFO/GFS 同窗对照与 4 个有界 action 的中立 portfolio；
- 再扩展到论文/官方完整窗口；
- 当前只进行有限、枚举式 action evaluation，不扩展为无边界策略搜索。

### 7.2 第二增量：GFS Adapter

最小接口：

```text
prepare_trace(window_spec) -> TraceManifest
simulate_policy(policy_spec, trace_manifest) -> RunManifest
extract_metrics(run_manifest) -> MetricsReport
compare_policies(metrics[]) -> ComparisonReport
audit_slo(metrics, slo_spec) -> AuditResult
```

每次 run 必须记录：GFS commit、Trace commit/hash、窗口、warm-up、policy spec、seed、命令、退出码、运行时、输出文件 hash。

### 7.3 V1 Agent Action Space

当前第一版只允许 4 个高层、有限、可枚举 profile：

- repository-default `fifo_spot`，guarantee rate 0.90；
- `spot_scheduler`，guarantee rate 0.80 / 0.90 / 0.95；
- guarantee horizon 固定 `[1]`，checkpoint interval 固定 3,600 秒。

不允许：node/GPU id、逐 Job placement、任意 Python、模型结构/训练超参、集群扩缩容、无边界自然语言参数。

preemption-cost、queue、placement 等 profile 只有在显式外部化、parity test 和真实双跑后才能进入 action space；当前没有把它们包装成可用能力。

### 7.4 V1 AgentTeams 映射（Manager、Worker 与 bridge 已接入）

- Manager：创建实验任务、收集结构化 artifact id、在全部 simulation 和 audit 完成后给出建议；
- Workload Analyst：只调用 `analyze_workload`，输出 `WorkloadSummary`；
- Scheduling Strategist：从允许的 action schema 生成 3～5 个 `PolicySpec`；
- Simulation Agent：每个候选在同一 `TraceManifest` 上独立运行 `simulate_policy`；
- SLO Auditor：执行确定性的 `audit_slo`，不能靠 LLM 主观判定；
- Human approval：在启动高成本全量 simulation 和接受最终策略前设 gate。

当前映射固定 AgentTeams `v1.2.1`，采用 1 个 Manager + 4 个 standalone Worker；Agent 之间只传 schema、fingerprint、状态和 artifact reference，大 CSV 不塞进自然语言上下文。finite task 使用 MinIO 目录、Matrix room 与官方 `manage-state.sh` 维护 `state.json`，并在高成本 simulation batch 和最终策略接受前保留 human approval。详见 `docs/agentteams-integration.md`。

2026-08-08 已验证 embedded Controller、CoPaw Manager 与四个 standalone Worker 全部为 `Running`，通过 AgentTeams/Higress Gateway 的最小请求确认响应模型为 `deepseek-v4-flash`。受限 MCP host bridge 已通过委托认证和真实 Workload Worker → Windows host → Alibaba Trace → 结构化 artifact 回读验证。四领域 finite project 已在首轮人工批准后完整执行并归档；全程没有新增 GFS run。Manager 的确定性 ranking 复现了现有账本结论：FIFO 在 allocation 阶段被排后，三个 GFS profile 在全部声明层级上并列。Admin 随后人工接受 `repository-default-gfs` 作为 V1 Demo 方案，项目 outcome 为 `accepted`；审计记录明确该选择不代表 0.90 在现有证据上优于 0.80/0.95。

### 7.5 V1 Demo 的完成定义

一次命令输入真实历史窗口和 SLO 文件，输出：

1. workload summary；
2. 3～5 个可执行 PolicySpec；
3. 同窗、隔离、可复现的模拟结果；
4. HP/Spot/资源指标对比；
5. 逐条 SLO pass/fail 与证据；
6. Manager 推荐和 human approval 状态。

V1 明确标注为 historical counterfactual optimization，不宣称 online scheduling。
