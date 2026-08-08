[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$forbiddenRoots = @(
    "AgentTeams/",
    "clusterdata/",
    "artifacts/",
    "dist/",
    ".venv/"
)
$forbiddenExtensions = @(".csv", ".zip", ".ckpt", ".pt", ".pth", ".pem", ".key", ".pfx")
$secretPattern = [regex]'(?i)(sk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{24,}|api[_-]?key\s*[:=]\s*[A-Za-z0-9._-]{16,})'
$files = @(
    & git -C $projectRoot ls-files --cached --others --exclude-standard |
        Where-Object { Test-Path -LiteralPath (Join-Path $projectRoot $_) -PathType Leaf }
)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to enumerate the public repository boundary."
}

$violations = [System.Collections.Generic.List[string]]::new()
foreach ($relative in $files) {
    $normalized = $relative.Replace("\", "/")
    if ($forbiddenRoots | Where-Object { $normalized.StartsWith($_, [System.StringComparison]::OrdinalIgnoreCase) }) {
        $violations.Add("forbidden root: $normalized")
        continue
    }
    $path = Join-Path $projectRoot $relative
    $extension = [System.IO.Path]::GetExtension($path).ToLowerInvariant()
    if ($extension -in $forbiddenExtensions -or [System.IO.Path]::GetFileName($path) -like ".env*") {
        $violations.Add("forbidden file type/name: $normalized")
        continue
    }
    $item = Get-Item -LiteralPath $path
    if ($item.Length -gt 2MB) {
        $violations.Add("file exceeds 2 MiB: $normalized")
        continue
    }
    if ($extension -notin @(".png", ".jpg", ".jpeg", ".gif", ".pdf") -and $secretPattern.IsMatch((Get-Content -Raw -LiteralPath $path))) {
        $violations.Add("possible credential: $normalized")
    }
}

foreach ($required in @(
    "LICENSE",
    "third_party/licenses/AgentTeams-Apache-2.0.txt",
    "third_party/manifest.json"
)) {
    if ($required -notin $files) {
        $violations.Add("missing third-party notice: $required")
    }
}

if ($violations.Count -gt 0) {
    $violations | ForEach-Object { Write-Error $_ }
    throw "Public repository boundary check failed."
}

[ordered]@{
    status = "passed"
    inspected_files = $files.Count
    raw_trace_files = 0
    upstream_source_trees = 0
    credential_findings = 0
} | ConvertTo-Json -Compress
