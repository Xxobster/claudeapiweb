# USER_GUIDE (v0.4.0)

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

## Projects (v0.3.0, API only; the screens come with the UI work package)
- `POST /api/projects` with `{name, path, remote_url?, profile?}` registers a project. The folder is only ever read. If it is a git repo, CPM reads the `origin` remote (credentials in the URL are removed); if there is none, the project shows `needs_remote: true` and you set it with `PUT /api/projects/{id}/remote`.
- CPM finds `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `MEMORY.md`, `.claude/rules`, `.claude/skills`, `.claude/settings*.json`, `.mcp.json`, `TODO.md` and `docs/TODO.md` (plus the profile's files, for example Nirandyr's `docs/JOB_BOARD.md`). It stores only a hash, never the text. Turn a source off or on with `PUT /api/projects/{id}/sources/{sid}`, rescan with `POST .../refresh`, add your own pattern with `POST .../patterns`.
- A new project is **not trusted**: its hooks, MCP servers, skills and settings files stay off and workers load no project settings. Trust it with `PUT .../trust {"trusted": true}` only for projects you wrote or reviewed.
- `profile: "nirandyr"` applies the Nirandyr profile (`profiles/nirandyr.yaml`). Removing a project (`DELETE`) only forgets it; your files are untouched.
- After pulling this version re-run `scripts\setup.ps1` (new dependency `pyyaml`).

## Tasks (v0.4.0, API only; the screens come with the UI work package)
All calls need the install token and CSRF header (see Security basics). A project must exist (see Projects above).
- Create: `POST /api/projects/{id}/tasks` `{title, description, priority, declared_scope: ["src/"], depends_on: [ids], ...}`. New tasks start in BACKLOG. Scope entries are project-relative (no `..`, no drive letters).
- Statuses (14): BACKLOG, READY, BLOCKED, RESERVED, RUNNING, TESTING, REVIEWING, CORRECTING, PAUSED, PAUSED_BUDGET, FAILED, COMPLETED, INTEGRATED, CANCELLED. `GET /api/task-states` lists the allowed moves.
- You can move a task to READY (it becomes BLOCKED with a reason if a dependency is not done), BACKLOG, PAUSED or CANCELLED with `POST /api/tasks/{id}/transition`. The other states are set by CPM's scheduler, workers and acceptance gate; nobody can mark a task completed by hand.
- Dependencies: `POST /api/tasks/{id}/dependencies {depends_on_id}`; a cycle is refused (409). `GET /api/projects/{id}/ready-queue` shows what can start (lower priority number first).
- Edit/reprioritize/reassign: `PATCH /api/tasks/{id}` (scope, model, effort and billing mode are locked while a worker is attached). Split: `POST /api/tasks/{id}/split`; merge: `POST /api/projects/{id}/tasks/merge`; cancel: `POST /api/tasks/{id}/cancel`.
- TODO import: `POST /api/projects/{id}/tasks/import-todo {source_name, text}` turns open Markdown checkboxes into BACKLOG tasks (re-importing is safe). `POST .../tasks/plan {prompt}` makes one task per line with the offline planner (no API call).
- Prompts: `POST /api/prompts {name, body}` stores a new version; bodies containing keys or tokens are refused.
- After pulling this version restart CPM: the database migrates to revision 0002 by itself.

## What exists in v0.1.0 (skeleton)
Only the skeleton: `/health`, database with all core tables, migrations, redacted logging. No API key is needed or read. The UI, security middleware and key wizard come in later work packages (PLAN.md WP2+).

## Developer commands
`pip install -e ".[dev]"`, `ruff check .`, `pytest -m "not windows"`.
