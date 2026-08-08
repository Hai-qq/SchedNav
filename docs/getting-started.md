# SchedNav 本地准备

本文只获取运行时依赖；第三方源码和 Trace 始终被 `.gitignore` 排除，不属于 SchedNav 仓库。

## 1. 获取固定版本的 GFS

在 SchedNav 根目录执行：

```powershell
git clone https://github.com/MachineLearningSystem/26ASPLOS-Spot.git 26ASPLOS-Spot
git -C 26ASPLOS-Spot checkout e998d5453e626a0b743b3fd5137c54c987db780b
git -C 26ASPLOS-Spot apply ..\patches\gfs\reproduction-gate.patch
git -C 26ASPLOS-Spot apply --reverse --check ..\patches\gfs\reproduction-gate.patch
```

最后一条命令只有在 patch 已正确应用时才通过。

## 2. 获取固定版本的 Alibaba GPU Trace

```powershell
git clone --filter=blob:none --no-checkout https://github.com/alibaba/clusterdata.git clusterdata
git -C clusterdata sparse-checkout init --cone
git -C clusterdata sparse-checkout set cluster-trace-v2026-spot-gpu
git -C clusterdata checkout 0d0f3f1efdbf1add6a7bcc63676eafbd1eb11f71
```

随后应存在：

```text
clusterdata/cluster-trace-v2026-spot-gpu/node_info_df.csv
clusterdata/cluster-trace-v2026-spot-gpu/job_info_df.csv
```

固定仓库树中未找到数据许可证，因此不得把这两份 CSV 或逐 Job 派生数据提交到 SchedNav。

## 3. 创建 Python 3.11 环境

```powershell
.\scripts\setup_gfs_runtime.ps1
$env:PYTHONPATH = (Resolve-Path .\src).Path
.\.venv-gfs\Scripts\python.exe -m schednav.cli validate `
  --config configs\baselines\stress-gpu-series-2-2024-04-12.json
```

`validate` 会核对 GFS commit、compatibility patch、Trace commit、必需文件和 simulator 参数。真实运行、MetricsReport、SLO audit 与 deterministic gate 命令见 `docs/reproduction-contract.md`。

## 4. AgentTeams

AgentTeams 独立安装，不复制进本仓库。版本、五角色映射、package 构建、MCP bridge 和 human approval 流程见 `docs/agentteams-integration.md`。所有角色的模型 ID 固定为 `deepseek-v4-flash`。
