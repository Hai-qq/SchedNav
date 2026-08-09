[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DatasetDirectory,
    [Parameter(Mandatory = $true)]
    [string]$AgentController,
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,
    [int]$Workers = 4
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Run scripts/setup_runtime.ps1 before the adaptive demo."
}
$dataset = (Resolve-Path -LiteralPath $DatasetDirectory).Path
$agent = (Resolve-Path -LiteralPath $AgentController).Path
$output = [System.IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $output) {
    throw "Refusing to overwrite output directory: $output"
}

$design = Join-Path $output "design"
$experiment = Join-Path $output "experiment"
$benchmark = Join-Path $output "adaptive-benchmark.json"
$receipt = Join-Path $output "adaptive-evidence.json"

& $python (Join-Path $PSScriptRoot "prepare_adaptive_study.py") `
    --project-root $projectRoot `
    --dataset-directory $dataset `
    --output-directory $design `
    --action-space configs/action_spaces/native-multiwindow-v3.json
if ($LASTEXITCODE -ne 0) { throw "Adaptive design preparation failed." }

& $python (Join-Path $PSScriptRoot "run_multiwindow_experiment.py") `
    --project-root $projectRoot `
    --dataset-directory $dataset `
    --output-directory $experiment `
    --action-space configs/action_spaces/native-multiwindow-v3.json `
    --selection-mode all-eligible `
    --selection-trace-id alibaba-gpu-series-2-adaptive-v3-design `
    --workers $Workers
if ($LASTEXITCODE -ne 0) { throw "All-window simulation failed." }

& $python (Join-Path $PSScriptRoot "evaluate_adaptive_benchmark.py") `
    --experiment-directory $experiment `
    --design (Join-Path $design "adaptive-study-design.json") `
    --rule-controller (Join-Path $design "workload-rule-controller.json") `
    --agent-controller $agent `
    --output $benchmark
if ($LASTEXITCODE -ne 0) { throw "Adaptive benchmark evaluation failed." }

& $python (Join-Path $PSScriptRoot "publish_adaptive_evidence.py") `
    --design (Join-Path $design "adaptive-study-design.json") `
    --benchmark $benchmark `
    --agent-controller $agent `
    --output $receipt
if ($LASTEXITCODE -ne 0) { throw "Adaptive evidence publication failed." }

[ordered]@{
    status = "completed"
    design = Join-Path $design "adaptive-study-design.json"
    benchmark = $benchmark
    evidence = $receipt
} | ConvertTo-Json -Compress
