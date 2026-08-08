# SchedNav 真实 Eviction Stress Gate

状态：最终 SLO 账本版已通过，2026-08-08。

## 目标

这个 gate 补足 A800 golden window 未发生抢占的覆盖缺口。它证明：在固定的真实 Alibaba Trace window、固定 GFS commit/patch 和 repository-default `spot_scheduler` 下，能够稳定复现来自 GFS 作业级 CSV 与结构化 event ledger 的 Spot 抢占证据。

它不证明 GFS 优于其他 scheduler，不把 Trace 启发式分数当作抢占事实，也不宣称 checkpoint 次数或硬件利用率可观测。Spot guarantee SLO 现由独立事件账本审计。

## 候选发现

`scan-windows` 使用只读、确定性的两阶段方法：

1. 对每个 GPU 型号和自然日计算前半日 Spot overlap pressure 与后半日 HP arrival pressure；
2. 只把启发式排名作为需要送进真实 GFS 的候选列表，是否发生抢占最终由仿真 CSV 决定。

当前排名公式为：

```text
early_spot_pressure
  = 前半日到达且与当天重叠的 Spot requested GPU-hours
    / (集群 GPU 数 * 12h)

late_hp_pressure
  = 后半日到达的 HP requested GPUs / 集群 GPU 数

candidate_score = early_spot_pressure * late_hp_pressure
```

扫描 2024-04-06 至 2024-08-31 后，`GPU-series-2 / 2024-04-12` 排名第 3，并且是前三名中最早的日期，因此在压力强度与完整仿真成本之间更合适。扫描报告保存在被忽略的 `artifacts/reproduction/eviction-window-candidates.json`，不会进入公开仓库。

## 固定配置

配置：`configs/baselines/stress-gpu-series-2-2024-04-12.json`。

- VC：`GPU-series-2`，122 个节点、976 张 GPU；
- trace origin：2024-03-01 00:00:00；
- evaluation：2024-04-12 00:00:00 至 23:59:59；
- prepared trace：5,792 HP（含 warm-up）与 evaluation 内 84 Spot；
- scheduler：GFS `spot_scheduler` repository-default profile；
- `guarantee_hours=[1]`，`guarantee_rate=0.9`，`ckpt_interval=3600s`；
- CPU deterministic profile，`num_workers=0`，seed `20260807`；
- drain 到所有已纳入作业结束。

## 通过条件

`eviction-gate` 只读取 canonical `MetricsReport`，并要求：

- metrics schema 与 fingerprint 有效；
- job/sequence/preemption-event CSV SHA-256 已进入 evidence；
- evaluation-window Spot population 非空且全部完成；
- event ledger 存在并与 Spot `preempt_times` 聚合一致；
- Spot `preemption_count > 0` 且 `preempted_job_count > 0`。

其中 `preemption_count` 是 evaluation arrival population 中 per-job `preempt_times` 之和，不读取自然语言日志里的 `Preemption_rate` 标签。

## 验证结果

最终 SLO 账本版隔离进程 `slo-v1-final-gfs-r1` 与 `slo-v1-final-gfs-r2` 均成功结束，comparison 为 `deterministic_match=true`，七个 CSV 的 SHA-256 分别为：

| Evidence | SHA-256 |
|---|---|
| job | `882750166e4463c2a599fb277e91ccf3cdf3c7509e3d23fe8dfa50f148f21f6f` |
| preemption event | `c796761e934a949b458015fd38b13b0368a4767157e0d94880f60ab6f84d8556` |
| Spot run-start event | `03d769f9e3afee3390aff0da42de2f02645851199c57abf645df9bd30c221f2d` |
| Spot guarantee event | `329a76f8b7a5c6356714623758d8a9ced438b296c5bcd2d05ef56f2ce7c59b05` |
| cluster sequence | `e260bbef17aeacd67f0030cd4abdbbee4a4794d4e0f0ace0fb78a7558e1505b7` |
| Spot quota | `8bf972bddb6a64ca627e302b2728a5bbbe047b6f824910f6d24d2ac9ed0d641e` |
| VC record | `83570768cb5d6224c888651ea51e0521a1e8ad054b325bb687ca6b6059eb6e1f` |

两次生成相同的 metrics fingerprint `ec9528e6cbaeaf8aeae2b6e282e921a21cec771890064c5f6f179361af44c77c`；r1 的 eviction gate fingerprint 为 `0ecd22028bff4a29c4f2aca9dd6600eab81fd0d3e508b025c17f42bd1dbe8952`。MetricsReport 同时固定 GFS commit、patch hash 和 Trace commit。

| 指标 | HP | Spot |
|---|---:|---:|
| job / completed | 94 / 94 | 84 / 84 |
| JCT mean | 8,644.989362s | 14,475.940476s |
| JCT p50 | 1,102s | 21,696s |
| JCT p95 | 44,972.1s | 21,962.1s |
| queue mean | 0s | 59.916667s |
| preemption count | 0 | 2 |
| preempted jobs | 0 | 1 |

Spot preempted-job rate 为 `1 / 84 = 0.0119047619`。1,440 个一分钟样本上的 mean GPU allocation rate 为 `0.733534`。两个事件合计 3,304 秒 rollback、80 秒 overhead、226,728 requested-GPU-seconds 新增工作量；这些是 simulator 记账，不是硬件能耗。以上数值描述当前单一策略和窗口，不是性能提升结论。

正式 SLO 口径下共有 86 次 Spot run-start 与 2 次 eviction，`spot_eviction_rate_per_run = 2/86 = 0.023255813953488372`；403 个 Spot guarantee events 中 401 成功、2 失败，`spot_guarantee_success_rate = 401/403 = 0.9950372208436724`。三类账本均与 job CSV 和彼此一致。

## 当前限制

- 上游 `ckpt_times` 只在 Trace 初始化时写为 0，全仓没有递增逻辑，因此不能作为 checkpoint count；
- ledger 已记录 preemption timestamp、cause、preemptor/preempted、rollback、overhead、remain 变化与新增 requested-GPU-seconds，但不能把后者解释为硬件 GPU core utilization 或能耗；
- GFS guarantee-hour succeed/failed 已由逐事件账本统一为正式 guarantee success rate；自然语言日志标签仍不作为证据输入；
- `preempt_times` 在其他上游 policy 中更新不一致，当前 gate 的抢占语义只覆盖已验证的 `fifo_spot` / `spot_scheduler` 路径。

同一 stress window 上的 FIFO/GFS 双运行和 4-policy portfolio 已完成；完整 trade-off 与不可归因限制见 `docs/policy-evaluation-contract.md`。

## 复现命令

以下命令中的 replicate id 必须使用尚不存在的新名称：

```powershell
$env:PYTHONPATH = "src"
.venv-gfs\Scripts\python.exe -m schednav.cli scan-windows `
  --trace-dir clusterdata\cluster-trace-v2026-spot-gpu `
  --earliest-date 2024-04-06 --latest-date 2024-08-31 --limit 20 `
  --output artifacts\reproduction\eviction-window-candidates.json
.venv-gfs\Scripts\python.exe -m schednav.cli run `
  --config configs\baselines\stress-gpu-series-2-2024-04-12.json --replicate stress-r1
.venv-gfs\Scripts\python.exe -m schednav.cli metrics `
  --config configs\baselines\stress-gpu-series-2-2024-04-12.json `
  --manifest artifacts\reproduction\runs\stress-gpu-series-2-2024-04-12-stress-r1\run_manifest.json `
  --output artifacts\reproduction\runs\stress-gpu-series-2-2024-04-12-stress-r1\metrics.json
.venv-gfs\Scripts\python.exe -m schednav.cli eviction-gate `
  --metrics artifacts\reproduction\runs\stress-gpu-series-2-2024-04-12-stress-r1\metrics.json `
  --output artifacts\reproduction\runs\stress-gpu-series-2-2024-04-12-stress-r1\eviction_gate.json
```
