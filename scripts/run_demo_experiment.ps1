param(
    [Parameter(Mandatory = $true)]
    [string]$DatasetDirectory,

    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,

    [string]$GpuModel = "GPU-series-2",
    [double]$EvaluationStartSeconds = 3628800,
    [double]$EvaluationEndSeconds = 3715199,
    [string]$TraceId = "alibaba-gpu-series-2-2024-04-12"
)

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$datasetRoot = (Resolve-Path -LiteralPath $DatasetDirectory).Path
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$nodeInfo = Join-Path $datasetRoot "node_info_df.csv"
$jobInfo = Join-Path $datasetRoot "job_info_df.csv"
$traceDirectory = Join-Path $outputRoot "trace"
$traceManifest = Join-Path $traceDirectory "trace.json"
$sloSpec = Join-Path $projectRoot "configs\slos\schednav-demo-slo-v1.json"
$policyIds = @(
    "native-fifo",
    "native-preemptive-0000",
    "native-preemptive-1800",
    "native-preemptive-3600"
)

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Missing project runtime: $python. Run scripts/setup_runtime.ps1 first."
}
if (-not (Test-Path -LiteralPath $nodeInfo -PathType Leaf) -or
    -not (Test-Path -LiteralPath $jobInfo -PathType Leaf)) {
    throw "DatasetDirectory must contain node_info_df.csv and job_info_df.csv."
}
if (Test-Path -LiteralPath $outputRoot) {
    throw "OutputDirectory already exists; choose a new directory: $outputRoot"
}
if ($EvaluationStartSeconds -lt 0 -or $EvaluationEndSeconds -le $EvaluationStartSeconds) {
    throw "Expected 0 <= EvaluationStartSeconds < EvaluationEndSeconds."
}

New-Item -ItemType Directory -Path $outputRoot | Out-Null
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = (Join-Path $projectRoot "src")

function Invoke-SchedNav {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & $python -B -m schednav.cli @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "schednav command failed with exit code ${LASTEXITCODE}: $($Arguments -join ' ')"
    }
}

try {
    Invoke-SchedNav import-alibaba `
        --node-info $nodeInfo `
        --job-info $jobInfo `
        --output-dir $traceDirectory `
        --trace-id $TraceId `
        --gpu-model $GpuModel `
        --evaluation-start-seconds $EvaluationStartSeconds `
        --evaluation-end-seconds $EvaluationEndSeconds

    Invoke-SchedNav analyze-trace `
        --trace $traceManifest `
        --output (Join-Path $outputRoot "workload-summary.json")

    foreach ($policyId in $policyIds) {
        $policyPath = Join-Path $projectRoot "configs\policies\$policyId.json"
        Invoke-SchedNav simulate `
            --trace $traceManifest `
            --policy $policyPath `
            --result (Join-Path $outputRoot "$policyId-result.json") `
            --metrics (Join-Path $outputRoot "$policyId-metrics.json")
        Invoke-SchedNav simulate `
            --trace $traceManifest `
            --policy $policyPath `
            --result (Join-Path $outputRoot "$policyId-r2-result.json") `
            --metrics (Join-Path $outputRoot "$policyId-r2-metrics.json")

        $first = Get-Content -LiteralPath (Join-Path $outputRoot "$policyId-metrics.json") -Raw |
            ConvertFrom-Json
        $second = Get-Content -LiteralPath (Join-Path $outputRoot "$policyId-r2-metrics.json") -Raw |
            ConvertFrom-Json
        if ($first.metrics_fingerprint -ne $second.metrics_fingerprint) {
            throw "Determinism check failed for $policyId."
        }
    }

    $metricsPaths = @($policyIds | ForEach-Object {
        Join-Path $outputRoot "$_-metrics.json"
    })
    Invoke-SchedNav compare-portfolio `
        --metrics @metricsPaths `
        --output (Join-Path $outputRoot "policy-portfolio.json")

    $baseline = Join-Path $outputRoot "native-fifo-metrics.json"
    $auditPaths = @()
    foreach ($policyId in $policyIds) {
        $auditPath = Join-Path $outputRoot "$policyId-slo-audit.json"
        Invoke-SchedNav audit-slo `
            --metrics (Join-Path $outputRoot "$policyId-metrics.json") `
            --slo $sloSpec `
            --baseline $baseline `
            --output $auditPath
        $auditPaths += $auditPath
    }

    Invoke-SchedNav rank-policies `
        --metrics @metricsPaths `
        --audits @auditPaths `
        --slo $sloSpec `
        --output (Join-Path $outputRoot "policy-ranking.json")

    $trace = Get-Content -LiteralPath $traceManifest -Raw | ConvertFrom-Json
    $ranking = Get-Content -LiteralPath (Join-Path $outputRoot "policy-ranking.json") -Raw |
        ConvertFrom-Json
    $manifest = [ordered]@{
        schema_version = "schednav.demo-experiment/v1"
        trace_id = $TraceId
        trace_fingerprint = $trace.trace_fingerprint
        evaluation_window_seconds = $trace.evaluation_window_seconds
        policy_ids = $policyIds
        repetitions_per_policy = 2
        deterministic_repetitions = $true
        selection_status = $ranking.selection_status
        ranking_fingerprint = $ranking.ranking_fingerprint
    }
    $manifest | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath (Join-Path $outputRoot "experiment-manifest.json") -Encoding utf8

    Write-Host "SchedNav demo experiment completed: $outputRoot"
    Write-Host "Selection status: $($ranking.selection_status)"
}
finally {
    $env:PYTHONPATH = $previousPythonPath
}
