[CmdletBinding()]
param(
    [string]$PythonCommand = "py"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$venv = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    & $PythonCommand -3.11 -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create the Python 3.11 environment."
    }
}

& $venvPython -c "import sys; assert sys.version_info[:2] == (3, 11), sys.version"
if ($LASTEXITCODE -ne 0) {
    throw "SchedNav requires Python 3.11."
}
& $venvPython -m pip install --disable-pip-version-check -e $projectRoot
if ($LASTEXITCODE -ne 0) {
    throw "Unable to install SchedNav in editable mode."
}

[ordered]@{
    status = "ready"
    python = $venvPython
    project_root = $projectRoot
} | ConvertTo-Json -Compress
