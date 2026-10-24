# USER_GUIDE (v0.2.0, WP2)

## Install (Windows 11, PowerShell 5.1)
1. Clone `https://github.com/Xxobster/claudeapiweb` to `D:\projects\ClaudeProjectManager` (never inside OneDrive; `setup.ps1` and the config loader refuse it).
2. `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` creates `.venv`, installs CPM and creates the database in `data\cpm.db`.
3. `powershell -ExecutionPolicy Bypass -File scripts\start.ps1` starts the server on `http://127.0.0.1:8765` and opens `/health` in the browser.

## Configuration (environment variables, all optional)
| Variable | Default | Note |
|---|---|---|
| `CPM_HOST` | `127.0.0.1` | Must be loopback; anything else is refused. |
| `CPM_PORT` | `8765` | |
| `CPM_DATA_DIR` | `<repo>\data` | DB and logs. Refused if the path contains `OneDrive`. |
| `CPM_LOG_LEVEL` | `INFO` | |

## Security basics (v0.2.0)
- CPM only answers on loopback and only to requests with the right Host header. API calls need the install token: run `powershell -File scripts\show_token.ps1` (the token is in `data\install_token`; never share it).
- `CPM_ALLOW_LIVE_KEY_TEST` (default off): set to `1` only when you want CPM to check your key against the real API (no tokens are spent). Without it the key check is offline.
- Your Anthropic API key is stored in Windows Credential Manager (service `ClaudeProjectManager`); it is never shown again, written to the database, logs or git. You can replace, test and remove it from the wizard (`/api/credentials/anthropic`, `/api/wizard/*`; the screen itself comes with the UI work package).
- If `ANTHROPIC_API_KEY` or similar variables are set in your Windows environment, CPM warns at startup and never passes them to workers.
- After pulling this version re-run `scripts\setup.ps1` (dependencies are now hash-locked and include `keyring`).

## What exists in v0.1.0 (skeleton)
Only the skeleton: `/health`, database with all core tables, migrations, redacted logging. No API key is needed or read. The UI, security middleware and key wizard come in later work packages (PLAN.md WP2+).

## Developer commands
`pip install -e ".[dev]"`, `ruff check .`, `pytest -m "not windows"`.
