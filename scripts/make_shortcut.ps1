# Creates 'Claude Project Manager' shortcuts (Desktop and Start Menu) that run scripts\start.ps1. Windows PowerShell 5.1.
# Remove them with -Remove. The shortcut only starts CPM; it stores no token or key.
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$start = Join-Path $root 'scripts\start.ps1'
$targets = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Claude Project Manager.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'Claude Project Manager.lnk')
)
foreach ($lnk in $targets) {
    if ($Remove) { if (Test-Path $lnk) { Remove-Item -LiteralPath $lnk }; continue }
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
    $sc.Arguments = "-NoProfile -File `"$start`""
    $sc.WorkingDirectory = $root
    $sc.Description = 'Start Claude Project Manager (local, 127.0.0.1:8765)'
    $sc.Save()
}
if ($Remove) { Write-Host "Shortcuts removed." } else { Write-Host "Shortcuts created: $($targets -join '; ')" }
