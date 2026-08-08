[CmdletBinding()]
param(
    [string]$BindAddress = "0.0.0.0",
    [ValidateRange(1, 65535)]
    [int]$Port = 18765,
    [string]$AuthGatewayUrl = "http://127.0.0.1:18080/v1/models",
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$config = Join-Path $projectRoot "configs\agentteams\host-bridge-native-v1.json"
$sourceRoot = Join-Path $projectRoot "src"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "SchedNav Python environment is missing: $python"
}
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
    throw "Host bridge config is missing: $config"
}
if ($env:SCHEDNAV_BRIDGE_TOKEN -or $env:SCHEDNAV_BRIDGE_TOKEN_FILE) {
    throw "Direct bridge-token environment variables must be unset; this launcher requires delegated AgentTeams authentication."
}

$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -Method Get -TimeoutSec 3
    }
    catch {
        throw "Port $Port is already in use by a service that is not a healthy SchedNav bridge."
    }
    if ($health.status -ne "ok" -or $health.service -ne "schednav-host-bridge") {
        throw "Port $Port returned an unexpected health document."
    }
    [ordered]@{
        status = "already_running"
        service = $health.service
        port = $Port
        listener_count = $listeners.Count
    } | ConvertTo-Json -Compress
    exit 0
}

if ($CheckOnly) {
    throw "SchedNav host bridge is not listening on port $Port."
}

$gatewayReachable = $false
try {
    $null = Invoke-WebRequest -Uri $AuthGatewayUrl -Method Get -TimeoutSec 5
    $gatewayReachable = $true
}
catch {
    $statusCode = $_.Exception.Response.StatusCode
    if ($statusCode -in @([Net.HttpStatusCode]::Unauthorized, [Net.HttpStatusCode]::Forbidden)) {
        $gatewayReachable = $true
    }
}
if (-not $gatewayReachable) {
    throw "AgentTeams authentication gateway is unavailable: $AuthGatewayUrl"
}

$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $sourceRoot
    & $python -m schednav.host_bridge `
        --project-root $projectRoot `
        --config "configs/agentteams/host-bridge-native-v1.json" `
        --bind $BindAddress `
        --port $Port `
        --auth-gateway-url $AuthGatewayUrl
    exit $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $previousPythonPath
}
