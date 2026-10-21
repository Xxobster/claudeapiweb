# Starts CPM on 127.0.0.1:8765 and opens the browser. Windows PowerShell 5.1 compatible.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw "Run scripts\setup.ps1 first." }
if ($env:ANTHROPIC_API_KEY -or $env:ANTHROPIC_AUTH_TOKEN) {
    Write-Warning "ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN is set in your environment. CPM never reads it; keys are entered in the app (docs/THREATS.md T-12)."
}
$port = if ($env:CPM_PORT) { $env:CPM_PORT } else { '8765' }
$hostName = if ($env:CPM_HOST) { $env:CPM_HOST } else { '127.0.0.1' }
if ($hostName -eq 'localhost') { $hostName = '127.0.0.1' }
if ($hostName.Contains(':')) { $hostName = "[$hostName]" }
$base = "http://${hostName}:$port"
$server = Start-Process -FilePath $python -ArgumentList @('-m', 'cpm.main') -PassThru -NoNewWindow
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try { Invoke-RestMethod -Uri "$base/health" -TimeoutSec 2 | Out-Null; $ready = $true; break } catch { }
}
if (-not $ready) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue; throw "CPM did not become healthy." }
Start-Process "$base/health"
Write-Host "CPM running (pid $($server.Id)). Close this window or press Ctrl+C to stop."
Wait-Process -Id $server.Id
