# One-step install: setup (venv, hash-locked deps, migrations), Desktop + Start Menu shortcuts, backup folder.
# Windows PowerShell 5.1 compatible. Safe to run again. Needs no admin rights and sends nothing over the network
# except pip downloading the pinned, hash-checked packages.
param([switch]$NoShortcut, [switch]$SkipSetup)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if ($root -match 'OneDrive') { throw "Install CPM in a local folder (D:\projects\ClaudeProjectManager), not OneDrive: OneDrive corrupts SQLite WAL files." }
if (-not $SkipSetup) { & (Join-Path $PSScriptRoot 'setup.ps1'); if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) { throw "setup failed" } }
if (-not $NoShortcut) { & (Join-Path $PSScriptRoot 'make_shortcut.ps1') }
New-Item -ItemType Directory -Force -Path (Join-Path $root 'data\backups') | Out-Null
Write-Host "CPM installed. Start it with the 'Claude Project Manager' shortcut or scripts\start.ps1."
Write-Host "Nothing was sent to Anthropic. Enter your API key only in the app's setup wizard (Settings)."
