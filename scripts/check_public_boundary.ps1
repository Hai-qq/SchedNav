[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
# Use the active environment's Python; one implementation on every platform.
& python (Join-Path $PSScriptRoot "check_public_boundary.py")
if ($LASTEXITCODE -ne 0) {
    throw "Public repository boundary check failed."
}
