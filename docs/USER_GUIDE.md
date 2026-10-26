# USER_GUIDE (v0.5.0)

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

## Workers and runs (v0.5.0, API only; the live log screen comes with the UI work package)
A run is one worker process for one task. It works in its own git worktree under `D:\projects\cpm-worktrees\<project>\task-<id>` on branch `claude/cpm-task-<id>-<title>`; your project folder is never written. The scheduler (WP7) will start runs by itself; until then you start them by hand.
- Start: `POST /api/tasks/{id}/runs`. The task must be READY (or PAUSED to resume). Tasks with `billing_mode: "mock"` run a scripted fake worker (no key, no money): body `{"scenario": "success|fail|hang|crash|budget_burst|grandchild|slow"}`.
- Live log: `GET /api/runs/{run}/events?since=<seq>` (polling) or the WebSocket `/api/runs/{run}/stream?since=<seq>` (needs the token header and an allowed Origin). Events are redacted and size-capped; `data\runs\<run>\events.jsonl` is the same log on disk.
- Stop: `POST /api/runs/{run}/stop` (or cancel the task) kills the worker and everything it started; the task becomes CANCELLED. Pause: `POST /api/runs/{run}/pause` (or move the task to PAUSED) asks the worker to stop, then kills it after 3 s; `POST /api/tasks/{id}/resume` continues the same session in the same worktree.
- A worker that finishes successfully moves its task to **TESTING**, not COMPLETED: only the acceptance gate (WP10) can complete a task. A crash, a silent hang (no heartbeat for 60 s) or a run over its time limit makes the task FAILED; retry with FAILED -> READY, the worktree and its files are kept. Work is never deleted automatically: `DELETE /api/tasks/{id}/worktree` refuses while the worktree has uncommitted changes or commits that are not merged anywhere else.
- **Real (API-billed) runs need all of these**: start CPM with `CPM_ALLOW_LIVE_RUNS=1`, finish the wizard (budget cap + key), install the SDK with `pip install -e ".[sdk]"`, and send `max_budget_microusd` with the start request (at most the task budget and at most `CPM_LIVE_RUN_MAX_MICROUSD`, default 2 000 000 = 2 USD, the approved first live test). The run stops by itself when the estimated spend reaches that cap (task becomes PAUSED_BUDGET). Do this only after you decide to run the first live test.
- Untrusted projects: the worker may edit files in its worktree and read the project, but cannot run tests or linters (they execute repository code); trust the project first to allow `python -m pytest|ruff|mypy`. Workers can never push, fetch, use the network, or touch `.git`, `.claude` or `.mcp.json`.
- Limits: `max_parallel_agents` from the wizard (default 3, hard maximum 8) counts running workers. Settings: `CPM_WORKTREE_ROOT` (default: `cpm-worktrees` next to the CPM folder, never inside OneDrive or inside the managed project).

## Reservations, Desktop claims and locks (v0.6.0)
- Before a worker starts, its task reserves what it will touch: its declared scope (files or folders; a folder blocks everything below it) and, for heavy lanes, `unity_editor`, `gpu`, `blender` or a device. Two tasks that overlap cannot run together; the second stays READY and shows why, for example: `blocked by: task #4 holds path src/ (needed src/a.py)` or `blocked by: Desktop claim on Assets/Game/UI/`. See it with `GET /api/tasks/{id}/conflicts`.
- **Tell CPM what Claude Desktop is editing**: `POST /api/projects/{id}/desktop-claims` with `{"path": "Assets/Game/UI/", "note": "menu rework", "expires_in_s": 7200}`; release with `DELETE /api/desktop-claims/{claim}`. A claim blocks CPM tasks that want to write there (reading is fine) and never stops a worker already running: the answer lists the tasks that already hold the path so you can pause them.
- **Heavy work waits**: while `BUILD_RUNNING.lock`, `BLENDER_RUNNING.lock` or a file in the gpu-lock folder exists, no heavy task starts; light tasks continue. At most one heavy task runs at a time. `GET /api/projects/{id}/locks` shows the state of these locks, the job board claims and the device locks (read-only).
- **Edits made outside CPM**: `POST /api/projects/{id}/external-scan` compares the project folder with the previous scan (first call = baseline) and lists which reserved paths were changed behind CPM's back. Nothing is overwritten or reverted. A stuck reservation can be released with `POST /api/tasks/{id}/reservations/release` (refused while a worker is running).
- Needs the PC once: confirm the real location of `BLENDER_RUNNING.lock` and share one real `docs/JOB_BOARD.md` (see DECISIONS 2026-10-26 WP6).

## What exists in v0.1.0 (skeleton)
Only the skeleton: `/health`, database with all core tables, migrations, redacted logging. No API key is needed or read. The UI, security middleware and key wizard come in later work packages (PLAN.md WP2+).

## Developer commands
`pip install -e ".[dev]"`, `ruff check .`, `pytest -m "not windows"`.
