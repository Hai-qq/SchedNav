# SchedNav Demo SLO v1

状态：已定稿、实现并通过正式同窗 4-policy 审计，2026-08-08。

## 审计对象

V1 评估同一个 Alibaba GPU Trace window 上的历史反事实策略。GFS 继续独占 Job-to-node/GPU placement；Agent 只能选择有限、版本化的高层 `PolicyAction`。所有候选必须使用同一 Trace、窗口、源码、补丁和执行控制，并与同窗 FIFO baseline 比较。

## 硬约束

违反任意一项即淘汰：

| 指标 | 阈值 |
|---|---:|
| HP 任务完成率 | `= 100%` |
| HP 被抢占任务数 | `= 0` |
| HP p95 JCT | `<= FIFO 同窗 p95 JCT * 1.01` |
| HP p95 排队时间 | `<= 3600s` |
| Spot 任务完成率 | `= 100%` |
| Spot eviction rate per run | `<= 10%` |
| Spot 保障时长兑现率 | `>= 90%` |
| GPU allocation rate | `>= FIFO 同窗 allocation rate` |

HP JCT 退化率定义为：

$$
\frac{\text{policy HP p95 JCT}-\text{FIFO HP p95 JCT}}
{\text{FIFO HP p95 JCT}}
\le 1\%.
$$

当前窗口 FIFO 的 HP p95 JCT 为 `45,012.1s`，因此代码使用的精确上限为 `45,462.221s`，展示时四舍五入为 `45,462.2s`。

## 正式事件口径

`spot_eviction_rate_per_run` 使用显式账本，不从作业级抢占率反推：

$$
\text{Spot eviction rate per run}
=
\frac{\text{Spot eviction event count}}
{\text{Spot run-start event count}}.
$$

每次 Spot 作业初次启动或被抢占后的恢复启动都产生一个 run-start event。分子、分母均按 evaluation arrival population 过滤，并包含该 population 在 drain 阶段发生的事件。

`spot_guarantee_success_rate` 同样使用逐次保障事件账本：

$$
\text{Spot guarantee success rate}
=
\frac{\text{succeeded guarantee events}}
{\text{succeeded guarantee events}+\text{failed guarantee events}}.
$$

每完成一个保障周期或作业在当前保障周期内正常结束记为成功；保障周期内被抢占记为失败。指标报告必须同时证明 run、preemption 和 guarantee 三类账本存在，并与作业汇总计数相互一致，否则硬审计失败。

GFS 论文将 eviction rate 定义为 Spot task 被驱逐次数除以运行次数；生产评估设置 `p=0.9`、使用 `3600s` 排队阈值，并报告生产环境 eviction rate 低于 `10%`。来源：[GFS paper](https://arxiv.org/html/2509.11134v1)。

## 软目标与排名

GPU allocation rate 的软目标为 `>= 80%`，未达到不直接淘汰。排名只在通过全部硬约束的候选之间进行：

1. 最大化 GPU allocation rate；
2. 与最高 allocation rate 的差严格小于 `0.01` 时，最小化 Spot p95 JCT；
3. 仍相同时，最小化 Spot eviction rate per run；
4. 仍相同则保留并列并请求 human approval，不增加未声明的第四级规则。

不使用由 LLM 自由赋权的综合分数。机器可执行定义位于 `configs/slos/schednav-demo-slo-v1.json`，审计输出遵循 `schemas/slo-audit.schema.json`，排名输出遵循 `schemas/policy-ranking.schema.json`。

## 正式审计结果

窗口为 `GPU-series-2 / 2024-04-12`，evaluation arrival population 为 94 HP 与 84 Spot。FIFO 与 repository-default GFS 均完成两次隔离运行；各自七类 CSV 的 SHA-256 在两次运行间完全一致。GFS 新账本补丁前后的五类既有 CSV 也完全一致。

| Profile | 硬约束 | HP p95 JCT | HP p95 queue | Spot p95 JCT | Evictions / runs | Guarantee success | Allocation | 80% 软目标 |
|---|---|---:|---:|---:|---:|---:|---:|---|
| FIFO 0.90 | 8/8 通过 | 45,012.1s | 0s | 40,686.0s | 0 / 84 = 0% | 401 / 401 = 100% | 9.0224% | 未达到 |
| GFS 0.80 | 8/8 通过 | 44,972.1s | 0s | 21,962.1s | 2 / 86 = 2.3256% | 401 / 403 = 99.5037% | 73.3534% | 未达到 |
| GFS 0.90 | 8/8 通过 | 44,972.1s | 0s | 21,962.1s | 2 / 86 = 2.3256% | 401 / 403 = 99.5037% | 73.3534% | 未达到 |
| GFS 0.95 | 8/8 通过 | 44,972.1s | 0s | 21,962.1s | 2 / 86 = 2.3256% | 401 / 403 = 99.5037% | 73.3534% | 未达到 |

四个 profile 的 HP/Spot completion rate 都是 100%，HP preempted job count 都是 0，allocation 都不低于同窗 FIFO。四者均通过全部硬约束，均未达到 80% allocation 软目标。

最终 portfolio fingerprint 为 `e70d33a1351480a0e1268b0df412f48aec3c19154e81f4a28416f3666b8b5c39`，ranking fingerprint 为 `9968b64e53a7b3fc968fe4cfea3bb09aa5e6df163e9760d3ff5d0a13f91fdfc2`。排名首先因 allocation rate 排除 FIFO；三个 GFS profile 的 allocation rate、Spot p95 JCT 与 eviction rate 均完全相同，因此最终状态是 `tie_requires_human_approval`。Manager 只能推荐 GFS 0.80/0.90/0.95 这个并列集合，不能在没有新增规则或新窗口证据时擅自选择其中一个。
