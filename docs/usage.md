# 运行与复现

```powershell
git clone https://github.com/Hai-qq/SchedNav.git
cd SchedNav
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:PYTHONPATH = (Resolve-Path .\src).Path
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

需要可训练的按租户预测器时，安装可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[forecast]"
# 或首次初始化时：.\scripts\setup_runtime.ps1 -Forecast
```

转换一个本地 Trace：

```powershell
schednav import-alibaba `
  --node-info C:\datasets\gpu-trace\node_info_df.csv `
  --job-info C:\datasets\gpu-trace\job_info_df.csv `
  --output-dir C:\datasets\schednav\gpu-series-2-window `
  --gpu-model GPU-series-2 `
  --evaluation-start-seconds 3628800 `
  --evaluation-end-seconds 3715199 `
  --exclude-warmup-spot
```

运行内置 simulator：

```powershell
schednav simulate `
  --trace C:\datasets\schednav\a100-day1\trace.json `
  --policy configs\policies\native-fifo.json `
  --result C:\datasets\schednav\fifo-result.json `
  --metrics C:\datasets\schednav\fifo-metrics.json
```

在某个决策截止时刻生成不含未来 Job 的预测输入与配额：

```powershell
schednav forecast-demand `
  --trace C:\datasets\schednav\a100-day1\trace.json `
  --controller configs\controllers\tenant-predictive-spot-v1.json `
  --cutoff-seconds 3628800 `
  --output C:\datasets\schednav\forecast.json
```

运行逐时刻预测控制 replay：

```powershell
schednav simulate-predictive `
  --trace C:\datasets\schednav\a100-day1\trace.json `
  --policy configs\policies\native-preemptive-g3600-b09-d0000.json `
  --controller configs\controllers\tenant-predictive-spot-v1.json `
  --result C:\datasets\schednav\predictive-result.json `
  --metrics C:\datasets\schednav\predictive-metrics.json
```

运行可恢复的预测多窗口研究：

```powershell
.\.venv\Scripts\python.exe .\scripts\run_predictive_multiwindow_experiment.py prepare `
  --dataset-directory C:\datasets\cluster-trace-v2026-spot-gpu `
  --output-directory C:\experiments\schednav-predictive-multiwindow

.\.venv\Scripts\python.exe .\scripts\run_predictive_multiwindow_experiment.py run-calibration `
  --output-directory C:\experiments\schednav-predictive-multiwindow --workers 2

.\.venv\Scripts\python.exe .\scripts\run_predictive_multiwindow_experiment.py freeze-selection `
  --output-directory C:\experiments\schednav-predictive-multiwindow

.\.venv\Scripts\python.exe .\scripts\run_predictive_multiwindow_experiment.py run-holdout `
  --output-directory C:\experiments\schednav-predictive-multiwindow --workers 2
```

同一个 Trace 可分别运行所选 Action Space 声明的有限策略，再交给 compare、SLO audit 与 ranking 命令处理。

[返回项目首页](../README.md)
