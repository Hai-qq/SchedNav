# SchedNav GFS Reproduction Contract

状态：V1 golden、eviction stress、FIFO/GFS 同窗确定性、4-policy portfolio 与 Demo SLO v1 审计/排名 gate 均已完成，2026-08-08。

## 目标与非目标

本合同证明同一真实 Trace、同一 GFS commit/patch、同一策略和同一 seed 在隔离进程中产生一致的 CSV evidence；stress gate 进一步要求结构化事件账本与作业级抢占计数一致。它不宣称完整 Alibaba 月度 baseline 已完成，也不改变 GFS 的 Job-to-GPU/Node placement。

AgentTeams 只编排这些 deterministic 工具，不替代证据 gate；在线调度不属于 V1。没有显式业务 SLO 时不生成最终推荐。

## 固定输入

- GFS commit：`e998d5453e626a0b743b3fd5137c54c987db780b`；许可证为 GPL-3.0-only；
- clusterdata commit：`0d0f3f1efdbf1add6a7bcc63676eafbd1eb11f71`；固定仓库树中未发现数据许可证，因此不再分发原始或派生 Trace；
- 原始 `submit_time` 是相对 2024-03-01 00:00:00 的秒数；V1 不允许把 trace start 平移到更晚日期，因为这需要重建窗口起点的 running/pending/checkpoint 状态。

第三方事实由 `third_party/manifest.json` 记录。GFS 源码和 Trace 都只作为本地依赖，不进入 SchedNav 仓库。公开仓库只保存 compatibility patch、固定版本/归属、第三方许可证和不含逐 Job 数据的最小结构化结果。

## Golden Window

当前配置为 `configs/baselines/golden-a800-2024-04-07.json`：

- VC：`A800-SXM4-80GB`，22 个节点、176 张 GPU；
- trace origin：2024-03-01 00:00:00；
- evaluation：2024-04-07 00:00:00 至 11:59:59；
- drain：停止接收窗口外作业后，继续运行到已纳入作业全部结束；
- scheduler：GFS `spot_scheduler` repository-default profile；
- device：CPU，`num_workers=0`，seed `20260807`。

选择 4 月 7 日是因为它满足当前 estimator 的最短历史合同，并在真实窗口内同时包含 HP 与 Spot。最短 warm-up 按当前实现计算：

```text
recorder initial skip 20h
+ seq_len 672h
+ pred_len 4h
+ validation 7d (168h)
= 864h
```

配置额外保留到 4 月 7 日，共 888 小时 warm-up。派生 golden CSV 只生成在被忽略的 `artifacts/` 下。

## Eviction Stress Window

配置为 `configs/baselines/stress-gpu-series-2-2024-04-12.json`：

- VC：`GPU-series-2`，122 个节点、976 张 GPU；
- evaluation：2024-04-12 全天；
- prepared trace：5,792 HP（含 warm-up）与 evaluation 内 84 Spot；
- 其余 scheduler、estimator、seed 和隔离设置与 golden gate 使用同一 repository-default contract。

该窗口由 `scan-windows` 启发式排名后送入真实 GFS 验证。启发式不构成抢占证据；只有 canonical MetricsReport 中 evaluation Spot 的 `preempt_times > 0` 才能通过 `eviction-gate`。详细方法与结果见 `docs/eviction-stress-gate.md`。

## Compatibility Patch

`patches/gfs/reproduction-gate.patch` 只处理运行契约：

- 声明缺失的 `chinese-calendar`；
- 补 `args.embed`；
- 外部化 trace end 与 evaluation range，同时拒绝语义错误的晚起点；
- 修正 `guarantee_hour` 为整数列表；
- 将 estimator checkpoint 目录名规范为跨平台的 `YYYYMMDDTHHMMSS`；
- 固定 Python、NumPy、PyTorch seed，并支持 deterministic CPU profile。
- 输出只读的结构化 preemption、Spot run-start 与 Spot guarantee event ledger，不改变 scheduler 决策；
- 将似然计算中 Softplus 后下溢为 0 的 scale clamp 到 dtype epsilon，避免合法 FIFO 训练在 PyTorch 分布校验中失败。

它不修改 scheduler、placer、quota 或 preemption cost。新账本补丁前后的五类既有 Spot CSV 完全一致；新增两类 CSV 只观察 run-start 与 guarantee 状态转换。FIFO 则正是触发 estimator 数值缺陷的路径。补丁基于 GPL-3.0-only 的 GFS；公开仓库保留上游归属、完整 GPL-3.0 文本和对应 patch。SchedNav 第一方代码的许可证需在比赛正式发布或接受外部贡献前单独决策。

## 隔离与证据

每个 replicate 使用：

- 独立 Python 进程；
- 独立 output 与 checkpoint 目录；
- 固定 `PYTHONHASHSEED`、单线程 BLAS 环境和配置 seed；
- `TraceManifest`：来源 commit/hash、筛选条件、行数和派生文件 hash；
- `RunManifest`：策略 fingerprint、命令、退出码和全部 CSV hash。
- `RunManifest` 同时记录 compatibility patch SHA-256，并校验上游只有补丁声明的 8 个 tracked 文件发生变化；
- `MetricsReport`：从 evaluation arrival population 和三类事件账本统一计算 HP/Spot JCT、queue、preemption、Spot evictions/runs、Spot guarantee success、rollback/overhead、requested-GPU-seconds 新增工作量与 allocation rate，不读取自然语言日志摘要；上游 `ckpt_times` 仍不可用。

日志含墙钟时间，不进入确定性比较。gate 只比较 GFS CSV evidence。

## Gate 命令

在项目根目录执行：

以下 replicate 名称假设对应输出目录尚不存在；adapter 会拒绝覆盖已有运行，请为重跑选择新的、可审计的 replicate id。

```powershell
$env:PYTHONPATH = "src"
.venv-gfs\Scripts\python.exe -m schednav.cli validate --config configs\baselines\golden-a800-2024-04-07.json
.venv-gfs\Scripts\python.exe -m schednav.cli prepare --config configs\baselines\golden-a800-2024-04-07.json
.venv-gfs\Scripts\python.exe -m schednav.cli run --config configs\baselines\golden-a800-2024-04-07.json --replicate r1
.venv-gfs\Scripts\python.exe -m schednav.cli run --config configs\baselines\golden-a800-2024-04-07.json --replicate r2
.venv-gfs\Scripts\python.exe -m schednav.cli compare `
  --first artifacts\reproduction\runs\golden-a800-2024-04-07-r1\run_manifest.json `
  --second artifacts\reproduction\runs\golden-a800-2024-04-07-r2\run_manifest.json `
  --output artifacts\reproduction\golden-a800-comparison.json
.venv-gfs\Scripts\python.exe -m schednav.cli metrics `
  --config configs\baselines\golden-a800-2024-04-07.json `
  --manifest artifacts\reproduction\runs\golden-a800-2024-04-07-r1\run_manifest.json `
  --output artifacts\reproduction\runs\golden-a800-2024-04-07-r1\metrics.json
.venv-gfs\Scripts\python.exe -m schednav.cli scan-windows `
  --trace-dir clusterdata\cluster-trace-v2026-spot-gpu `
  --earliest-date 2024-04-06 --latest-date 2024-08-31 --limit 20 `
  --output artifacts\reproduction\eviction-window-candidates.json
.venv-gfs\Scripts\python.exe -m schednav.cli eviction-gate `
  --metrics artifacts\reproduction\runs\stress-gpu-series-2-2024-04-12-r1\metrics.json `
  --output artifacts\reproduction\runs\stress-gpu-series-2-2024-04-12-r1\eviction_gate.json
```

## 通过条件

只有全部成立才称为 golden gate 通过：

1. 两次退出码均为 0；
2. Trace、policy、run-spec fingerprint 相同；
3. 两次 CSV 文件集合相同；当前 stress contract 包含 job、preemption event、Spot run-start event、Spot guarantee event、cluster sequence、Spot quota、VC record 七类 CSV；
4. 每个对应 CSV 的 SHA-256 完全相同；
5. 两次使用不同输出与 checkpoint 目录；
6. 没有把 `artifacts/`、上游源码或 Trace 加入 SchedNav 版本库。

完整 Alibaba 月度 baseline 与全量性能优化仍是后续 gate，不能由本次结果推导。

stress gate 另要求 MetricsReport fingerprint 与 CSV evidence hash 有效、Spot population 非空且全部完成，并至少有一个 Spot 作业的 `preempt_times > 0`。

## 当前验证结果

2026-08-07 在 Windows、Python 3.11、CPU deterministic profile 上运行 golden `r3-attested` 与 `r4-attested`：

- 两次均独立训练 estimator 并以退出码 0 完成；
- run-spec fingerprint：`0c92d0e5d5aefea2f94b2323830234d77038f7ce1566348adb1192d7e37ba0a4`；
- 当时 compatibility patch SHA-256：`c518f550eb6eb29e30b356ba98b6baebdf6d7e21c028c10349a43a79a6386b86`；随后 stress ledger/numerical 版本为 `43392f5f920f2c1fc2a673cf5a2c0d51a2e5bbe4df5cefb1af56be1e97798477`；
- comparison：`deterministic_match=true`，4 个 CSV 无差异；
- evaluation population：5 HP、27 Spot，全部完成；
- HP JCT mean/p50/p95：10,969.6 / 3,877 / 35,400.4 秒；HP queue 全为 0；
- Spot JCT mean/p50/p95：1,174.444444 / 1,089 / 1,664.7 秒；Spot queue 全为 0；
- Spot preemption count：0；
- 720 个一分钟样本上的 mean GPU allocation rate：0.222185。

这些 metrics 只描述当前小型 repository-default golden window，不是 scheduler 对比结果。由于没有发生抢占，它本身不能证明 eviction、checkpoint rollback 或 Spot guarantee failure 路径正确；其中 eviction 覆盖缺口已由下述独立 stress gate 补足。

同日完成的 current-patch eviction stress 双运行产生 `deterministic_match=true`：evaluation population 为 94 HP 与 84 Spot，全部完成；1 个 Spot 作业累计发生 2 次抢占，两个运行的 eviction gate 均通过。Spot preempted-job rate 为 0.0119047619，mean allocation rate 为 0.733534。事件账本记录 3,304 秒 rollback、80 秒 overhead 和 226,728 requested-GPU-seconds 新增工作量。

同窗 FIFO 双运行也产生 `deterministic_match=true`，且 4-candidate portfolio `comparable=true`。FIFO/GFS delta 与 quantile 无差异结果见 `docs/policy-evaluation-contract.md`。这些是完整 profile 的实测 trade-off，不是已应用业务 SLO 的 winner 或性能提升宣称。

2026-08-08 的最终 SLO 账本版 GFS 0.90 双运行再次得到 `deterministic_match=true`，七类 CSV 全部一致；compatibility patch SHA-256 为 `623ceda161983b150b5fb339e4755016ce121c12b86e7fc6f5382dc7f6e54ae1`。既有五类 CSV hash 与上段旧实验完全一致。新账本得到 86 次 Spot run、2 次 eviction，正式 eviction rate 为 `2/86 = 0.023255813953488372`；403 个保障事件中 401 成功、2 失败，兑现率为 `0.9950372208436724`。对应 MetricsReport fingerprint 为 `ec9528e6cbaeaf8aeae2b6e282e921a21cec771890064c5f6f179361af44c77c`。

FIFO 最终账本版双运行同样得到七类 CSV `deterministic_match=true`。GFS 0.80/0.95 的最终独立运行与 0.90 除 quota CSV 外的六类 evidence 完全相同。4-policy portfolio `comparable=true`，fingerprint 为 `e70d33a1351480a0e1268b0df412f48aec3c19154e81f4a28416f3666b8b5c39`；四个候选均通过 8 项硬 SLO，均未达到 80% allocation 软目标。排名 fingerprint 为 `9968b64e53a7b3fc968fe4cfea3bb09aa5e6df163e9760d3ff5d0a13f91fdfc2`，最终保留 GFS 0.80/0.90/0.95 三方并列并请求 human approval。

## 已知限制与保留现场

- 上游 `logger_init` 在同一进程向 root logger 叠加 handler，导致部分人类可读日志重复；CSV evidence 不受影响，日志也不参与确定性比较。
- 首次运行 `r1` 暴露并记录了 Windows checkpoint 路径失败；后续成功运行和最终 attested 运行都使用独立目录，没有覆盖失败证据。
- 本地保留失败运行、两次前置成功运行和两次 attested 运行；它们全部位于被忽略的 `artifacts/`，清理前应先由用户确认。
- SchedNav 第一方代码许可证尚未选择；当前公开可见代码不附加许可，必须在比赛正式发布或接受外部贡献前结合 GFS GPL-3.0 patch 边界作出明确决定。
- 上游 `ckpt_times` 从未递增，因此 checkpoint count 仍不可报告。event ledger 已提供 preemption timestamp、cause、rollback、overhead 和新增 requested-GPU-seconds，但该数值不是硬件能耗或 GPU core utilization；正式 eviction/guarantee 指标不依赖 `ckpt_times`。
