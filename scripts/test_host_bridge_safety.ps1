[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 18765,
    [switch]$SkipLive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "SchedNav Python environment is missing: $python"
}

$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = (Join-Path $projectRoot "src") + [IO.Path]::PathSeparator + (Join-Path $projectRoot "tests")
    & $python -m unittest -v `
        test_host_bridge.HostBridgeTests.test_operation_contract_rejects_unlisted_arguments `
        test_host_bridge.HostBridgeTests.test_simulation_rejects_unlisted_action_profile `
        test_host_bridge.HostBridgeTests.test_audit_rejects_unknown_slo_spec `
        test_host_bridge.HostBridgeTests.test_mcp_lists_and_calls_bounded_tools_with_bearer_auth
    if ($LASTEXITCODE -ne 0) {
        throw "Host bridge safety unit tests failed."
    }
}
finally {
    $env:PYTHONPATH = $previousPythonPath
}

if (-not $SkipLive) {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -Method Get -TimeoutSec 3
    if ($health.status -ne "ok" -or $health.service -ne "schednav-host-bridge") {
        throw "Live bridge health check returned an unexpected document."
    }

    $unauthorized = $false
    try {
        $null = Invoke-WebRequest `
            -Uri "http://127.0.0.1:$Port/mcp" `
            -Method Post `
            -ContentType "application/json" `
            -Body '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' `
            -TimeoutSec 3
    }
    catch {
        if ($_.Exception.Response.StatusCode -eq [Net.HttpStatusCode]::Unauthorized) {
            $unauthorized = $true
        }
        else {
            throw
        }
    }
    if (-not $unauthorized) {
        throw "Live bridge accepted an MCP request without bearer authentication."
    }

    $probe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $probe.Start()
    $unavailablePort = ([Net.IPEndPoint]$probe.LocalEndpoint).Port
    $probe.Stop()
    $connectionFailed = $false
    try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:$unavailablePort/healthz" -Method Get -TimeoutSec 2
    }
    catch {
        $connectionFailed = $true
    }
    if (-not $connectionFailed) {
        throw "Unavailable-bridge probe unexpectedly returned a response."
    }
}

[ordered]@{
    schema_version = "schednav.host-bridge-safety-check/v1"
    bounded_arguments_rejected = $true
    unlisted_action_rejected = $true
    unknown_slo_rejected = $true
    missing_or_invalid_bearer_rejected = $true
    unavailable_bridge_fails_closed = (-not $SkipLive)
    live_bridge_checked = (-not $SkipLive)
} | ConvertTo-Json
