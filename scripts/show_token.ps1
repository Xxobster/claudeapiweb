# Prints the CPM install token (needed as X-CPM-Token by API clients). Local use only; never paste it anywhere.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dataDir = if ($env:CPM_DATA_DIR) { $env:CPM_DATA_DIR } else { Join-Path $root 'data' }
$file = Join-Path $dataDir 'install_token'
if (-not (Test-Path $file)) { throw "No token yet: start CPM once (scripts\start.ps1)." }
Get-Content $file
