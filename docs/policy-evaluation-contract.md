# SchedNav V1 Policy Evaluation Contract

## 边界

V1 是 historical trace-driven counterfactual evaluation，不是 online scheduling。LLM/Agent 只能选择版本化的高层 `PolicyAction`；GFS 独占 Job-to-node/GPU placement。策略优劣只能来自同一真实 Trace window 上的隔离仿真和显式 SLO 审计。

## 有限 Action Space

当前 `configs/action_spaces/v1-baseline.json` 只允许 4 个精确、版本化 profile：

- repository-default FIFO：`fifo_spot / guarantee_rate=0.9`；
- repository-default GFS：`spot_scheduler / guarantee_rate=0.9`；
- GFS quantile 0.80：`spot_scheduler / guarantee_rate=0.8`；
- GFS quantile 0.95：`spot_scheduler / guarantee_rate=0.95`；
- 四者均固定 `guarantee_hours=[1]`、`ckpt_interval_seconds=3600` 和相同执行控制。

`job_id`、`node_id`、`gpu_id`、placement、任意 Python、estimator 架构和训练超参数均禁止进入 Agent action。validator 只接受与 `profiles` 中某一项完全一致的 action，不能从各字段的 allowed value 私自拼接新组合。

两个 repository-default profile 已各完成两次隔离运行并通过逐文件确定性比较；0.80 与 0.95 profile 各完成一次隔离 evidence run。action-space 状态 `simulation-evidence-available` 不表示每个 profile 都已双跑，也不表示任何 profile 更优。

## 执行链

```text
PolicyAction
  -> finite action validation
  -> materialized RunSpec + receipt
  -> fresh GFS subprocess
  -> RunManifest + hashed CSV evidence
  -> canonical MetricsReport
  -> same-window neutral ComparisonReport
  -> 3-5 candidate PortfolioReport
  -> explicit SLO Audit
```

每个 candidate 使用新的进程、Trace/Cluster 状态、输出目录和 checkpoint 目录。已存在的 run directory 不允许覆盖。失败运行保留 manifest、stdout 和 stderr，不能进入策略比较。

## 可比性 Gate

`compare-policies` 仅在以下条件全部成立时返回 `comparable=true`：

- 两份 metrics schema 与 fingerprint 有效；
- GFS commit、compatibility patch hash 与 Trace commit 相同；
- Trace ID、evaluation window 和 HP/Spot population 相同；
- 两侧 population 均完整结束；
- preemption、Spot run-start 与 Spot guarantee 三类 event ledger 均存在且相互一致；
- 两侧高层 policy action 不同，固定执行控制相同。

报告只给出 `right - left` 的绝对/相对 delta 和指标偏好方向，不输出 winner。`preferred_direction` 不是 SLO，也不能替代业务权衡。

`compare-portfolio` 接受 3～5 份 canonical MetricsReport，增加 action 唯一性、共同源码、共同执行控制和全部 pairwise comparability gate；输出候选指标表与全部两两 delta，同样不排序、不选择 winner。

## 指标解释限制

上游 `fifo_spot` 与 `spot_scheduler` 同时存在队列排序、placement 和 `worker_num` 请求记账差异。因此同窗对比是两份完整上游实现的 profile comparison，不是某一个启发式的单变量消融；特别是 `allocation_rate_mean` 不能单独解释为调度算法提升。

抢占事件账本只重放调度器已经计算的事件字段：时间、cause、preemptor/preempted、rollback、overhead、remain 变化和新增 requested-GPU-seconds。它不改变 scheduler 决策，也不等于硬件 GPU core utilization 或训练有效吞吐。

## SLO Gate

`audit-slo` 只接受显式 `schednav.slo-spec/v1`。正式口径是 `configs/slos/schednav-demo-slo-v1.json`，完整公式与阈值见 `docs/schednav-demo-slo-v1.md`：

- 任一 hard constraint 失败，candidate 被淘汰；
- soft constraint 失败只作为显式 tradeoff；
- 指标不可用时记为 unavailable/failed；
- 相对阈值必须提供同源码、同 Trace、同窗口、同 population 的 canonical FIFO metrics；
- 三类事件账本缺失或不一致时，硬审计失败。

项目不从 Trace 分位数或期望结果倒推业务阈值。硬约束通过后只执行 SLO 声明的 allocation rate → Spot p95 JCT → Spot eviction rate 分层排名，不使用 LLM 自由加权分数；若仍并列则请求 human approval。

## 当前真实窗口结果

最终账本版 `GPU-series-2 / 2024-04-12` 4-candidate portfolio 已通过可比性 gate 与 SLO 审计。该窗口包含 94 个 HP 与 84 个 Spot，均全部完成。

| Profile | HP p95 JCT | Spot p95 JCT | Eviction / run | Guarantee success | Allocation rate | Hard SLO |
|---|---:|---:|---:|---:|---:|---|
| FIFO 0.90 | 45,012.1s | 40,686.0s | 0% | 100% | 0.090224 | 通过 |
| GFS 0.80 | 44,972.1s | 21,962.1s | 0.0232558 | 0.9950372 | 0.733534 | 通过 |
| GFS 0.90 | 44,972.1s | 21,962.1s | 0.0232558 | 0.9950372 | 0.733534 | 通过 |
| GFS 0.95 | 44,972.1s | 21,962.1s | 0.0232558 | 0.9950372 | 0.733534 | 通过 |

GFS 0.80/0.90/0.95 的 quota CSV hash 不同，但其余六类 GFS evidence 和 canonical 排名指标完全相同。分层排名先排除 allocation 较低的 FIFO，随后在三个 GFS profile 之间无法继续决胜，返回 `tie_requires_human_approval`。这不能解释为 quantile 不重要；只表示当前窗口没有区分它们的证据。FIFO 与 GFS 是完整上游 profile 对比，仍受上述实现差异解释限制。
