# GFS compatibility patch

`reproduction-gate.patch` 基于 `MachineLearningSystem/26ASPLOS-Spot` commit `e998d5453e626a0b743b3fd5137c54c987db780b`，仅提供 SchedNav 可复现运行与审计所需的兼容层。

当前补丁 SHA-256：`623ceda161983b150b5fb339e4755016ce121c12b86e7fc6f5382dc7f6e54ae1`。

补丁内容：

- 补齐依赖、确定性 seed、CPU profile、外部 Trace/evaluation window 和跨平台 checkpoint 路径；
- 将 estimator 的退化 Normal scale 夹到当前 dtype 的 machine epsilon；
- 输出只读 preemption event、Spot run-start event 和 Spot guarantee event 三类结构化账本；
- 账本只重放 scheduler 已发生的状态转换，不参与 placement、quota、队列顺序、preemption cost 或候选选择。

在干净的固定 commit 上应用：

```powershell
git apply ..\patches\gfs\reproduction-gate.patch
```

可用以下命令验证补丁与当前已修改工作树严格对应：

```powershell
git apply --check --reverse ..\patches\gfs\reproduction-gate.patch
```

上游 GFS 使用 GPL-3.0-only。公开分发本补丁时必须保留上游归属并遵守 GPL-3.0；SchedNav 仓库不复制或提交完整 GFS 源码。
