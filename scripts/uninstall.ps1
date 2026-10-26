# Removes the shortcuts and the virtual environment. It NEVER deletes data\ (database, backups, logs) or worktrees:
# those hold your work. Delete them by hand if you really want them gone. Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot 'make_shortcut.ps1') -Remove
$venv = Join-Path $root '.venv'
if (Test-Path $venv) { Remove-Item -LiteralPath $venv -Recurse -Force }
Write-Host "Removed shortcuts and .venv. Kept: data\ and the worktree folder."
