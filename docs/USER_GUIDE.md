# USER_GUIDE (v0.15.0)

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

## Workers and runs (v0.5.1, API only; the live log screen comes with the UI work package)
A run is one worker process for one task. It works in its own git worktree under `D:\projects\cpm-worktrees\<project>\task-<id>` on branch `claude/cpm-task-<id>-<title>`; your project folder is never written. The scheduler (WP7) will start runs by itself; until then you start them by hand.
- Start: `POST /api/tasks/{id}/runs`. The task must be READY (or PAUSED to resume). Tasks with `billing_mode: "mock"` run a scripted fake worker (no key, no money): body `{"scenario": "success|fail|hang|crash|budget_burst|grandchild|slow"}`.
- Live log: `GET /api/runs/{run}/events?since=<seq>` (polling) or the WebSocket `/api/runs/{run}/stream?since=<seq>` (needs an allowed Origin plus the token header, or a one-time `?ticket=` from `POST /api/ws-ticket`). Events are redacted and size-capped; `data\runs\<run>\events.jsonl` is the same log on disk.
- Stop: `POST /api/runs/{run}/stop` (or cancel the task) kills the worker and everything it started; the task becomes CANCELLED. Pause: `POST /api/runs/{run}/pause` (or move the task to PAUSED) asks the worker to stop, then kills it after 3 s; `POST /api/tasks/{id}/resume` continues the same session in the same worktree.
- A worker that finishes successfully moves its task to **TESTING**, not COMPLETED: only the acceptance gate (WP10) can complete a task. A crash, a silent hang (no heartbeat for 60 s) or a run over its time limit makes the task FAILED; retry with FAILED -> READY, the worktree and its files are kept. Work is never deleted automatically: `DELETE /api/tasks/{id}/worktree` refuses while the worktree has uncommitted changes or commits that are not merged anywhere else.
- **Real (API-billed) runs need all of these**: start CPM with `CPM_ALLOW_LIVE_RUNS=1`, finish the wizard (budget cap + key), install the SDK with `pip install -e ".[sdk]"`, and send `max_budget_microusd` with the start request (at most the task budget and at most `CPM_LIVE_RUN_MAX_MICROUSD`, default 2 000 000 = 2 USD, the approved first live test). All runs of one task (retries, resumes) share that ceiling: a new run is refused if the task has already spent too much. A project whose folder is no longer inside an approved root cannot start workers. The run stops by itself when the estimated spend reaches that cap (task becomes PAUSED_BUDGET). Do this only after you decide to run the first live test.
- Untrusted projects: the worker may edit files in its worktree and read the project, but cannot run tests or linters (they execute repository code); trust the project first to allow `python -m pytest|ruff|mypy`. Workers can never push, fetch, use the network, or touch `.git`, `.claude` or `.mcp.json`.
- Limits: `max_parallel_agents` from the wizard (default 3, hard maximum 8) counts running workers. Settings: `CPM_WORKTREE_ROOT` (default: `cpm-worktrees` next to the CPM folder, never inside OneDrive or inside the managed project).

## Reservations, Desktop claims and locks (v0.8.0; screen in v0.10.0)
On the **Agents** page, each project shows what its tasks have reserved, your Desktop claims (add a path and a note, release with one click; if a running task already holds the path you get a warning to pause it), the state of the project's own locks and a button that scans the main folder for edits made behind CPM's back. A task page says why the task cannot start and offers "Release this task's reservations" for a stuck one (refused while a worker runs). The API calls behind it:
- Before a worker starts, its task reserves what it will touch: its declared scope (files or folders; a folder blocks everything below it) and, for heavy lanes, `unity_editor`, `gpu`, `blender` or a device. Two tasks that overlap cannot run together; the second stays READY and shows why, for example: `blocked by: task #4 holds path src/ (needed src/a.py)` or `blocked by: Desktop claim on Assets/Game/UI/`. See it with `GET /api/tasks/{id}/conflicts`.
- **Tell CPM what Claude Desktop is editing**: `POST /api/projects/{id}/desktop-claims` with `{"path": "Assets/Game/UI/", "note": "menu rework", "expires_in_s": 7200}`; release with `DELETE /api/desktop-claims/{claim}`. A claim blocks CPM tasks that want to write there (reading is fine) and never stops a worker already running: the answer lists the tasks that already hold the path so you can pause them.
- **Heavy work waits**: while `BUILD_RUNNING.lock`, `BLENDER_RUNNING.lock` or a file in the gpu-lock folder exists, no heavy task starts; light tasks continue. At most one heavy task runs at a time. `GET /api/projects/{id}/locks` shows the state of these locks, the job board claims and the device locks (read-only).
- **Edits made outside CPM**: `POST /api/projects/{id}/external-scan` compares the project folder with the previous scan (first call = baseline) and lists which reserved paths were changed behind CPM's back. Nothing is overwritten or reverted. A stuck reservation can be released with `POST /api/tasks/{id}/reservations/release` (refused while a worker is running).
- Needs the PC once: confirm the real location of `BLENDER_RUNNING.lock` and share one real `docs/JOB_BOARD.md` (see DECISIONS 2026-10-26 WP6).
## The web interface (v0.7.0)
Start CPM with `scripts\start.ps1`: it opens `http://127.0.0.1:8765/` already signed in (the token travels in the address fragment and is removed from the bar). If a tab asks for the token, paste the output of `scripts\show_token.ps1`. "Lock" (top right) forgets the token in this browser; the pill next to it shows whether CPM is reachable.
- **Settings**: save/test/remove the Anthropic key (never shown again) and the first-run setup (folder, caps, parallel agents). No billed run is possible without a key and a monthly cap.
- **Projects**: add a folder (read-only registration). Each project has a Board (one column per status, refreshes by itself), a Table, "Sources & trust" and "Add tasks" (new task or TODO checklist import).
- **Trust**: until you trust a project, its workers do not load its CLAUDE.md, hooks, MCP servers or skills. Trust it only after looking at the repository, because its scripts then run on this PC.
- **Task page**: move it between states, start a run (mock tasks pick a scenario; API tasks need a per-run budget), pause/resume/cancel, and follow the worker's log live. A finished worker leaves the task in TESTING.
- **Agents**: workers running now, the budget they hold, and per project the reservations, your Desktop claims and the project's own locks (see below). **Costs**: see "Budget and cost center". **Git & review**: branch state per project; diffs and PRs arrive later.
- Open two tabs on two projects at once; each follows its own runs.
- Check the UI yourself: `node tests/ui/e2e.mjs` (Node + Playwright installed; uses mock runs only).
- After pulling this version re-run `scripts\setup.ps1` (new dependency `websockets`).

## Budget and cost center (v0.9.0; screen in v0.10.0)
On the **Costs** page: the month's meter (spent, held by running workers, cap, 70 / 90 / 100 % marks and what each level does), a form to add to this month's cap, per-project spend and room left, the price table, your credit balance and the reconcile form. A task that ran out of budget shows the five choices at the top of its task page (and is listed on Costs); "Add budget" asks you to confirm the amount, then press "Resume" on the task page to run it again. The monthly cap itself is set in **Settings**. The API calls behind it:
CPM keeps one ledger of every message's cost and refuses to start a run that would not fit. Every number is an **estimate** (token counts times the price table) until you reconcile it with the Console.
- Where you see it: `GET /api/budget` (month, per project, held money, level 0/70/90/100, credit balance), `GET /api/tasks/{id}/budget`, `GET /api/budget/reservations` (money held by running workers).
- Caps: the wizard sets task, project and monthly caps. Task = whole life of the task; project and month reset each UTC month. Without a monthly cap no billable run starts.
- Starting a run holds its per-run budget; if the month, project, task or credit balance cannot cover it you get HTTP 409 saying which ceiling, how much is left and what you asked for. The hold is given back when the run ends, however it ends.
- 70 % of a cap: information; 90 %: alert (the run log marks `checkpoint_requested`); 100 %: the worker is stopped, the worktree is kept, the task becomes **PAUSED_BUDGET** and nothing more is spent without you.
- Your five choices for a PAUSED_BUDGET task: `POST /api/tasks/{id}/budget-action` with `add_budget` (amount_usd, raises this task's cap), `resume_no_increase` (checks the headroom after you fixed an accounting error), `reassign_model` (cheaper model), `defer` (wait for next month), `cancel` (keeps the work). Then resume with `POST /api/tasks/{id}/resume`; the earlier spend still counts.
- Other tools: `POST /api/budget/top-up {scope: month|project|task, ref, amount_usd}`, `PUT /api/budget/credit-balance {balance_usd}` (Anthropic does not expose your credit; CPM shows entered balance minus spend since), `POST /api/budget/reconcile {actual_usd, period?}` (type the Console figure; the month then shows "reconciled"), `GET/PUT /api/budget/prices` (appends a new price version; check the prices at each release).
- Limits: no daily cap yet, no automatic Console reconciliation, SDK `max_budget_usd` is only a backstop. Auto-reload of credits is never used.
- After pulling this version restart CPM: the database migrates to revision 0004 by itself.

## Scheduler (v0.11.0)
CPM starts READY tasks by itself when it is run with `CPM_SCHEDULER=1` (`scripts\start.ps1` does this). Every 2 seconds it takes the READY tasks in order (priority number first, then the shorter heavy job, then id), checks the limits (your `max_parallel_agents`, one heavy task at a time), then reserves the files/tools the task declared and starts a worker. If a task cannot start it stays READY and the reason is shown ("blocked by: ..." or "deferred: ...").
- `GET /api/scheduler`: running tasks, limits, last tick result. `POST /api/scheduler/pause` / `resume`: stop or restart starting new work (running workers are not touched). `POST /api/scheduler/tick`: one pass now.
- Failed tasks: mock tasks are retried automatically up to 2 times (30 s apart), but never when the same failure repeats twice; then they stay FAILED with a `scheduler.gave_up` note. API-billed tasks are not retried by themselves (a retry costs money); change with `PUT /api/scheduler/config` (`retry_billing_modes`).
- Tasks waiting for a dependency are moved to READY when it is done. PAUSED and PAUSED_BUDGET tasks wait for you.
- Scheduler screen (v0.11.1, menu "Scheduler"): pause/resume, "Run one pass", the global / per-project / heavy limits, the workers running now, the queue in start order with the reason each task waits ("waiting: ...", "blocked by: ...", "deferred: ..." and the backoff time left after a refused start), the failed tasks with what automatic retry will do, and the form for the limits. The button for automatic retry of API tasks is off by default; turning it on asks for confirmation because every retry spends from the budget. The page refreshes every 3 s.
- Starting a task by hand (`POST /api/tasks/{id}/runs`) skips the reservation check: prefer letting the scheduler do it.

## Review, acceptance gate and pull requests (v0.13.0)
When a worker finishes, its task waits in **TESTING**. The **acceptance gate** then checks, in this order: the task has acceptance criteria; the worker changed something; no file is outside the declared scope or protected (`.git`, `.claude`, `.mcp.json`, symlinks, submodules, files over 5 MB); the project's tests pass (only in a trusted project; the first command found, or `test_commands` in the project profile); the result still merges onto the base branch; an independent reviewer marks every criterion met. Only then the task becomes **COMPLETED**.
- If tests fail, a file is unexpected or the reviewer rejects, the findings go back to a worker (**CORRECTING**) for up to 2 corrections (Git & review page, "Corrections per task"). The same failure twice in a row, or the limit, ends the task as FAILED.
- The gate **waits** (task stays TESTING, the reason is shown) when: no acceptance criteria (edit the task, then run the gate again), the project is not trusted but has tests, the branch conflicts with the base (rebase it in your own tree; CPM never rebases), or no automatic reviewer exists. API-billed tasks have no automatic reviewer yet: read the diff on the task page, then press "Approve after my review".
- Task page, section **Review**: the changed files (with "expected" or the reason a file is unexpected), the diff per file or whole, every gate result with tests, review and findings, and the **Publish** box. Viewing never changes the task's files.
- **Publish** (COMPLETED tasks): save a fine-grained GitHub token first (Git & review page; needs contents and pull requests write on the project repository). The button commits exactly what the gate checked, pushes the `claude/...` branch (never main, never forced) and opens a pull request that lists files changed, tests, security findings, exploit findings and what still needs the PC (the text you type). CPM never merges: merge it on GitHub. A failed attempt can be retried; nothing is pushed twice.
- API: `GET /api/tasks/{id}/review|changes|diff`, `POST /api/tasks/{id}/gate|gate/approve|integrate`, `GET/PUT /api/gate/config`, `PUT/DELETE /api/credentials/github`. The gate runs by itself on every unpaused scheduler pass (turn off with `auto` in the gate settings).

## Job board claims (v0.14.0)
Nirandyr rule 40: other threads must see which files a job holds. On a task's page, the **Job board** box opens a docs-only pull request that adds one row (task, `cpm` as owner, the task's declared scope, `claimed`) to the project's `docs/JOB_BOARD.md`; merge it on GitHub to make the claim visible. When the work is finished, **Release the claim** opens a second pull request that marks the row `released` (it needs the claim PR to be merged first). Nothing is claimed or released automatically, and CPM never merges.
- Needs: a saved GitHub token (Git & review page), a project with a profile that names a job board (Nirandyr does), an `origin` remote on github.com and an up-to-date `origin/main` in your clone (fetch it in your own tree; CPM does not). CPM refuses when the board has no table with files / status / job columns, when the scope overlaps another owner's active claim, or when the task has no declared scope.
- API: `GET /api/tasks/{id}/job-board`, `POST /api/tasks/{id}/job-board/claim|release`.
## Lanes (v0.16.0)
A **lane** is a recipe for one kind of work (voice lines, 3D assets, maps, Unity checks). It lives in the project profile (`profiles/<project>.yaml`, key `lanes:`); CPM ships the framework and the Nirandyr **voice** lane (below); the 3D, map and Unity lanes come next. A lane says:
- **task_template**: title prefix, description, inputs you must fill (e.g. `scene`), default scope and acceptance criteria;
- **required_tools / required_paths**: absolute paths that must exist on the PC. If one is missing the task is not started (it waits, nothing is charged);
- **resources**: `gpu`, `unity_editor`, `blender`, `device`... These make the task heavy: one at a time, never while `BUILD_RUNNING.lock` or another Nirandyr lock exists;
- **commands**: the only extra commands the worker may run (exact program, listed options, paths inside its worktree). Everything else stays refused;
- **artifacts**: folders outputs must go to, allowed extensions, size limit and the **binary policy** (`ask` = the gate stops and asks you, `lfs_only`, `deny`);
- **validators**: checks run by the acceptance gate on the outputs (`files_exist`, `file_size`, `name_pattern`, `json_keys` built in; voice/3D/map/Unity ones come with their lanes);
- **report**: headings the worker's final answer must contain; **limits**: run time and hang timeout; **queue_bias**: number added to the task priority in the scheduler (higher = later).
Use it: `GET /api/projects/{id}/lanes` lists a project's lanes (and what is missing on this PC), `POST /api/projects/{id}/lanes/{lane}/tasks` with `{"title": ..., "inputs": {...}}` creates a task from the template (it starts in BACKLOG like any task). In the gate, failed checks go back to the worker as a correction; binaries awaiting your decision, or checks that cannot run on this PC, leave the task in TESTING with the reason. Lane screen (v0.15.1): project page, tab "Lanes", shows each lane's resources, allowed commands, validators, artifact and binary policy, report format and whether this PC is ready (missing tools/paths); read-only, the profile is the only place a lane changes. A task that has a lane shows a lane box on its page, and the acceptance gate history shows the lane checks (passed / failed / waiting, findings, binaries awaiting your decision).

### Voice lane (`voice`, Nirandyr, v0.17.0)
Renders the fixed voice lines with Nirandyr's own local pipeline (free, no paid voice API) and checks the result. Create a task with `POST /api/projects/{id}/lanes/voice/tasks`.
- **Commands** the worker may run, exactly these two: `python tools/voice/render_fixed.py --check` and `python tools/voice/render_fixed.py` (no other flag, script or tool; trusted workspace only). Model download, story QC and any other script stay refused.
- **Priority**: the lane holds the `gpu` resource (one heavy task at a time, never while `BUILD_RUNNING.lock` or a gpu-lock exists) and has `queue_bias: 500`, so every ordinary task, build or art task goes first (Nirandyr rules 26, 27, 49). A voice task that is waiting simply starts as soon as the GPU is free; the render resumes (already valid lines are skipped). A voice task that FAILED is not restarted for you while it is API-billed (that would spend money again): press retry, or add `api` to the scheduler's `retry_billing_modes` if you want rule 49 fully automatic.
- **Validators** (all read-only, standard library only): `audio_format` (sample rate, channels, bit depth, duration range), `audio_loudness` (peak/clipping and gated RMS level in dBFS, an approximation, not LUFS), `audio_silence` (all-silent file, silence share, leading/trailing silence), `voice_line_list` (every id of the script/index has a non-empty output file, ids are validated, stray outputs are a warning). Audio checks parse WAV only; `.rva` files are checked for existence and completeness, not decoded.
- **Binaries**: `.rva`/`.wav` outputs use the `ask` policy: the gate stops and lists them, you decide (Nirandyr rules on binaries).

## Restart and backups (v0.12.0)
- When CPM starts it first copies the database to `data\backups\` (the newest 10 are kept), then checks for work that was interrupted by a stop or crash. Interrupted tasks are marked **FAILED** (or **PAUSED** if they were in the middle of a commit: check the task's worktree, then resume or retry yourself). Locks and budget holds of those tasks are freed. Nothing is started or completed by this check.
- What was found is at `GET /api/recovery` (last report and backup list); `POST /api/recovery/reconcile` repeats the check and `POST /api/recovery/backup` makes a backup now.
- Tasks that were being reviewed go back to TESTING (the gate asks again); a task that was waiting for its correction worker is PAUSED.
- Worktrees left behind are listed in the report with "has work ... kept" or "clean". CPM never deletes them by itself.
- Restore a backup: stop CPM, then `python -m cpm.recovery list` and `python -m cpm.recovery restore data\backups\<file>`. The database you replaced is kept next to it as `cpm.db.before-restore-<time>`.

## What exists in v0.1.0 (skeleton)
Only the skeleton: `/health`, database with all core tables, migrations, redacted logging. No API key is needed or read. The UI, security middleware and key wizard come in later work packages (PLAN.md WP2+).

## Developer commands
`pip install -e ".[dev]"`, `ruff check .`, `pytest -m "not windows"`.

## Nirandyr lanes: 3D, maps, Unity validation (v0.16.0)
Three lanes ship in `profiles/nirandyr.yaml`. Create a task with `POST /api/projects/{id}/lanes/{lane}/tasks` (lane = `three_d`, `maps` or `unity_validation`); it starts in BACKLOG like any task and is heavy (one at a time, never while `BUILD_RUNNING.lock` or another Nirandyr lock exists).
- **three_d**: the worker runs the project's Blender batch script (fixed arguments) and writes models to `Assets/Art/Generated/`. The gate checks that each model opens and its triangle count, UVs, scale, size and names; binaries wait for your decision.
- **maps**: the worker only runs `tools/sim/zone_gen.sh --world` to verify the frozen map and writes a report under `docs/jobs/map_verify/`. Any change to a golden file fails the task.
- **unity_validation**: the worker runs Edit Mode tests in batch mode on its own worktree. The gate reads the test XML and editor log (failed tests, compile errors, missing scripts) and checks `.meta` files (missing, orphan, duplicate GUID).
Before the first run, look at the profile once on the PC: the Blender path and script names, the generator options and the golden paths are assumptions (a wrong one makes the task wait or fail with a clear reason, it never runs something else).
