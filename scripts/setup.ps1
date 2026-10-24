# One-time setup: creates .venv, installs CPM, applies migrations. Windows PowerShell 5.1 compatible.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if ($root -match 'OneDrive') { throw "Run CPM from a local folder (D:\projects\ClaudeProjectManager), not OneDrive." }
$py = (Get-Command py -ErrorAction SilentlyContinue)
if ($py) { & py -3.12 -m venv .venv } else { & python -m venv .venv }
if ($LASTEXITCODE -ne 0) { throw "venv creation failed (Python 3.12 required)" }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw "pip install (hash-locked) failed" }
& .\.venv\Scripts\python.exe -m pip install --no-deps -e .
if ($LASTEXITCODE -ne 0) { throw "pip install -e failed" }
& .\.venv\Scripts\python.exe -c "from cpm.config import load_settings; from cpm.db.migrate import upgrade; s=load_settings(); s.data_dir.mkdir(parents=True, exist_ok=True); upgrade(s.db_url); print('Database ready:', s.db_path)"
if ($LASTEXITCODE -ne 0) { throw "migration failed" }
Write-Host "Setup done. Start with scripts\start.ps1"
