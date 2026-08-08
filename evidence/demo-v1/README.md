# SchedNav Demo v1 Evidence

本目录只保存可公开的结构化结果，不包含 Alibaba 原始 Trace、逐 Job CSV、GFS 源码、日志或 checkpoint。

所有策略使用同一个 `GPU-series-2 / 2024-04-12` Trace Window。四份 MetricsReport 分别对应 FIFO、GFS guarantee rate 0.80、repository-default 0.90 和 0.95；四份 SLO audit 与它们一一对应。`policy-portfolio.json` 证明候选 population 可比，`policy-ranking.json` 按硬 SLO → allocation rate → Spot p95 JCT → eviction rate 的声明顺序排名。

当前三个 GFS quantile 在排名指标上并列。`repository-default-gfs` 是 human approval 后的接受结果，不表示 0.90 优于 0.80 或 0.95。

原始运行与全量 CSV 只存在于本地研究工作区，并由 RunManifest/TraceManifest hash 锁定。公开结果的 schema 位于 `schemas/`；复现实验所需上游版本和命令位于 `docs/reproduction-contract.md`。
