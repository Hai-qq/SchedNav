# SchedNav 文档

[项目首页](../README.md) · [贡献指南](../CONTRIBUTING.md) · [研究结果](research-results.md)

## 第一次使用

1. 按[首页快速体验](../README.md#快速体验)安装并运行合成对照样例。
2. 阅读[Trace Contract](trace-contract.md)，理解数据来源、语义与 fingerprint。
3. 按[运行与复现](usage.md)转换自己的数据并运行仿真。

## 按任务阅读

| 主题 | 入口 |
|---|---|
| 数据格式与来源许可 | [Trace Contract](trace-contract.md) · [Datasets](datasets.md) |
| 调度和资源语义 | [Native simulator](native-simulator.md) |
| 指标、约束和裁决 | [Demo SLO](schednav-demo-slo-v1.md) |
| 预测模型、配额与反馈 | [Predictive control](predictive-control.md) |
| 隐藏未来与连续状态 | [Rolling control](rolling-control.md) |
| 外部 Agent 运行环境 | [AgentTeams integration](agentteams-integration.md) |
| 研究设计与证据 | [Multiwindow evaluation](multiwindow-evaluation.md) · [Adaptive holdout](adaptive-holdout-evaluation.md) · [Research results](research-results.md) |

## 阅读证据

`evidence/` 保存汇总回执，不包含原始 Trace 或逐 Job 数据。不同代际研究绑定不同实现与窗口；合成样例、历史真实数据实验和生产部署是不同验证范围。

预测和多 Agent 的性能优势尚未建立。结果为并列、无合格策略或 `approval_pending` 时，应保留原裁决。
