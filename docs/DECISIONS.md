# DECISIONS (Micah's answers win)

## 2026-10-21 — answers to PLAN.md section 1
- General: "ok pour tes recommandations" — every recommendation in PLAN.md section 1 is accepted: Q1 (API spending allowed under a hard monthly cap Micah sets in the app; mocks first; first live smoke test limited to about 2 USD, Haiku/Sonnet by default), Q2 (code in `D:\projects\ClaudeProjectManager`, OneDrive folder = docs only), Q4 (no paid third-party media API in v1), Q5 (branch + PR only, fine-grained GitHub token in Windows Credential Manager), Q6 (job board read-only in v1), Q7 (worktrees under `D:\projects\cpm-worktrees\`, one persistent Unity validation worktree), Q8 (3 coding agents, max 1 heavy task), Q9 (CLI subscription mode = interface only), Q10 (English UI), Q11 (no autostart).
- Project folder: Claude work happens in `D:\projects\ClaudeProjectManager` (approved on the device). CLAUDE.md, PLAN.md and docs are copied there; the OneDrive copy stays as a mirror of the documents.
- Q3 (tools on the PC), partial answer:
  - Unity 6000.3.25f1: Start Menu folder `C:\Users\xxobs\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Unity 6000.3.25f1`. The real `Unity.exe` path is still to be found by WP0 (usually `C:\Program Files\Unity\Hub\Editor\6000.3.25f1\Editor\Unity.exe`; must be verified, not assumed).
  - Blender 5.2: `C:\Program Files\Blender Foundation\Blender 5.2\blender-launcher.exe` (for batch/background runs use `blender.exe` in the same folder with `-b`; WP0 verifies).
  - Still unknown: local voice pipeline (TTS/training tools), the map generator and the 3D generation scripts in Nirandyr. WP0 inspects `D:\projects\game\tools\` read-only to list them, and the lane packages (WP13/WP14) use that list.
- Still open for Micah: the monthly cap amount (Q1).

## 2026-10-21 (later) — Q1 answered
- Micah: yes to API spending and to the 2 USD first live smoke test. "We must always know the remaining budget and work according to the plan; if the tokens run out there is a described procedure."
- Applied: CPM always shows remaining budget (global, project, task) and admits work only if it fits; at exhaustion it follows PLAN.md WP8 / master prompt section 12 (PAUSED_BUDGET, preserved worktree, handoff note per CLAUDE.md rule 15).
- The monthly cap amount was not given: it is set by Micah in the first-run wizard. Until a cap is configured, the scheduler admits no live (billable) run; mock runs are unaffected.

## 2026-10-21 (later) — Unity path
- Unity.exe: `D:\Unity\Editors\6000.3.25f1\Editor\Unity.exe` (given by Micah; WP0 only confirms it runs).

## 2026-10-21 (WP0) — findings awaiting Micah (recommendation applies until answered)
- Branch: the GitHub repo has no `main`; the default branch is `claude/project-thread-px0aun`. WP0 PR targets it. Recommended: create `main` from it after merge.
- T-20: Nirandyr `docs/procedures/api_parallel.md` forbids Unity/Blender/GPU for API jobs. Recommended: allow CPM lanes to use them under devlock / gpu_lock / BUILD_RUNNING, and update that procedure via a Nirandyr PR. Lanes WP13/WP14 wait for this answer.
- Confirmed paths: Unity.exe `D:\Unity\Editors\6000.3.25f1\Editor\Unity.exe`; Blender 5.2.2 `blender.exe` in `C:\Program Files\Blender Foundation\Blender 5.2\`.
- Maps: no overworld map generator found in Nirandyr tools, only the dungeon generator (`tools/sim/dungeon_gen.sh`). Question: is the dungeon generator the "map" lane target?

## 2026-10-21 (WP0 answers) — Micah: "je suis d'accord avec tout"
- T-20 accepted: CPM lanes may use Unity, Blender and GPU tools under devlock / gpu_lock / BUILD_RUNNING. A Nirandyr PR must update `docs/procedures/api_parallel.md` accordingly (to do, not done by CPM).
- Create `main` on `Xxobster/claudeapiweb` from the planning branch after PR #1 merges (not done yet).
- Map lane target (answer from the Nirandyr side, relayed by Micah): `tools/sim/zone_gen.sh` (`--world` for the world map) with `tools/sim/zonegen/village_gen.py`, `mountain_caves.py`, its tests and golden files `golden_frozen_v1.txt`, `golden_layouts*.txt`. `dungeon_gen.sh` is a separate "dungeons" lane. The map lane only runs the generator and tests, compares with the golden files and reports differences, never fixes (map frozen, Nirandyr decision 413). `zone_gen.sh` waits while `BUILD_RUNNING.lock` exists; no adb, no GPU. Not verified by WP0 (not found in the local checkout); WP14 must confirm the paths on `main` of Nirandyr.

## 2026-10-24 (WP2) — implementation choices (awaiting Micah only if he disagrees)
- Auth model: random install token in `data\install_token` sent as `X-CPM-Token`; CSRF token derived by HMAC and sent as `X-CPM-CSRF` on POST/PUT/PATCH/DELETE; Host must be `127.0.0.1|localhost|[::1]:<port>`; Origin, when present, must be the same. Only `GET /health` is public. No cookies, no CORS.
- Key storage: `keyring` (Windows Credential Manager). No plaintext fallback: if no secure backend exists the API answers 503. Key format check rejects admin keys (`sk-ant-admin...`).
- Key validation is a Mock by default. The live check (`GET /v1/models`, no token spend) needs `CPM_ALLOW_LIVE_KEY_TEST=1` plus `live: true` in the request, i.e. Micah's go.
- Wizard: global budget must be > 0 (this is the mandatory cap of Q1); task <= project <= global; `max_parallel_agents` 1..8 (default 3, Q8); project dir becomes the first approved root and may not overlap the data dir.
- `mypy --strict` now runs in CI (deferred from WP1). Dependencies are hash-locked in `requirements.lock` (universal, Python 3.12); `setup.ps1` installs from it.
- Version 0.2.0.

## 2026-10-21 (WP1) — implementation choices
- Python target 3.12 (the PC has 3.12.10); code also runs on 3.13 in the cloud. Package name `claude-project-manager`, import name `cpm`, `src/` layout.
- Money stored as integer micro-dollars (`*_microusd`); status columns are plain strings until WP4.
- `/health` is unauthenticated in WP1; WP2 adds token/Origin/Host checks and decides what stays public.
- `ruff` + `pytest` in CI; `mypy --strict` from PLAN.md is deferred to WP2 (the first package with non-trivial logic), noted here so it is not forgotten.
- Alembic baseline `0001` was autogenerated from the models and is excluded from ruff.

## 2026-10-26 (WP4) — implementation choices (awaiting Micah only if he disagrees)
- Initial task status is BACKLOG (MASTER_PROMPT list); the WP1 placeholder DRAFT is migrated away (revision 0002).
- Priority: lower number runs first (default 100, range 0-10000). Ready queue order: priority, then id.
- Dependencies count as satisfied only when the upstream task is COMPLETED or INTEGRATED. Cancelling a task blocks its BACKLOG/READY dependents with a reason; nothing is cancelled or completed automatically.
- Humans can only request BACKLOG, READY, BLOCKED, PAUSED, CANCELLED. RESERVED/RUNNING/TESTING/REVIEWING/CORRECTING/PAUSED_BUDGET/FAILED/COMPLETED/INTEGRATED belong to scheduler, workers and the acceptance gate; COMPLETED additionally needs gate evidence.
- Split cancels the original and creates subtasks (they inherit dependencies; dependents of the original now wait for all parts). Merge cancels the originals and creates one task (union of scope/acceptance, minimum priority). Both only for BACKLOG/READY/BLOCKED tasks and both roll back on a cycle.
- TODO import: Markdown checkboxes only (`- [ ]`, `1. [ ]`), nesting becomes `parent_id`, `[x]` items skipped, code fences ignored, re-import adds nothing (hash per project). The API takes the file text; reading the file from a managed repo is WP3 (discovery).
- AI planner: only the deterministic MockPlanner exists; `live: true` answers 501 until WP5 and Micah's budget go.

## 2026-10-26 (WP5) — implementation choices (awaiting Micah only if he disagrees)
- Process model: the supervisor starts `python -I -m cpm.workers.run --spec <file>` with `cwd = data\runs\<run>` (never the worktree, so a file in the managed repo cannot shadow a module), an explicit environment, and a Job Object on Windows (process group on Linux). The child prints JSON lines; the supervisor is the only database writer.
- Stop semantics: cancel/hang/timeout/budget/shutdown kill the tree immediately; pause writes `stop` on the child's stdin and kills after 3 s. Pause + resume = a new run (attempt n+1) that resumes the previous SDK session in the same worktree.
- A successful worker moves the task to TESTING, never COMPLETED (acceptance gate = WP10). Failure, crash, hang and timeout -> FAILED (retry via READY keeps the worktree); budget stop -> PAUSED_BUDGET (resuming from it belongs to WP8).
- Live (API) runs need `CPM_ALLOW_LIVE_RUNS=1`, wizard cap, key in the store, and an explicit per-run budget not above `min(CPM_LIVE_RUN_MAX_MICROUSD = 2 USD, wizard task budget)`. Nothing here calls the API without those; no live run was made in WP5.
- Cost estimate per message uses the SDK_NOTES section 5 prices (integers, never under-counts unknown models); since v0.8.0 (WP8) it comes from the DB price table (`budget/pricing.py`). Usage rows dedup on the message id; a final reported total above the sum adds a reconciling row, except for resumed sessions (their total includes earlier runs).
- Workers get no auto-approved tools: every file and Bash call goes through `workers/permissions.py`. Bash = one simple allowlisted command (git read/add/commit, `ls`, `pwd`, and in a trusted workspace `python -m pytest|ruff|mypy`). Lanes (WP12) will add their own allowlists.
- Worktree base is `origin/main` when it exists, otherwise HEAD. Content filters (LFS) are blanked for checkout, so LFS files appear as pointers until WP12 decides the binary policy.
- WebSocket progress is a 0.2 s tail of the JSONL file, so reconnecting with `since=<seq>` replays exactly.

## 2026-10-26 (WP6) — implementation choices (awaiting Micah only if he disagrees)
- Reservations: one row per (task, resource). A task's set is granted atomically or not at all, and re-acquiring replaces it. Paths: exclusive/shared, prefix overlap, per project. Named machine-wide resources: `unity_editor`, `gpu`, `blender`, `device:<serial>`, `build`, `test_env`, `custom`; `git_branch:<name>` is per project.
- Max 1 heavy task is enforced twice: by the implicit `class:heavy` exclusive reservation (atomic) and by the scheduler (WP7). Heavy types come from the profile (`heavy_resources`).
- Lease 120 s by default (max 3600); a lapsed lease is removed only when the holder is not alive, otherwise it is renewed.
- Task `declared_scope` stays a list of plain project-relative paths (a trailing `/`, `/*` or `/**` means a folder). Lane/machine resources are passed by the scheduler or lane (`extra`), not typed by the user.
- Desktop claims block exclusive requests only, never stop a running worker, and live in CPM's database (the job board in the managed repo stays read-only, rule 40 / Q6).
- External locks (Nirandyr rules 13, 24, 26, 27): `BUILD_RUNNING.lock`, `BLENDER_RUNNING.lock` and any `*.lock` in `D:\projects\gpu-locks` block heavy tasks (the voice job uses gpu_lock.sh, so it is covered by the folder); job-board rows block overlapping exclusive paths; devlock files are listed for display and device commands must go through `devlock.py run` (CPM builds the argv, never runs the repo script itself).
- Open for the PC (Micah, no hurry, safe default applies): confirm where `BLENDER_RUNNING.lock` and any separate voice lock really live (assumed `D:\projects\game-build\BLENDER_RUNNING.lock`; voice assumed covered by gpu-locks), and show one real `docs/JOB_BOARD.md` so the parser can be checked against it. The parser fails closed (unknown status = claimed).
- External edits are found by polling git (HEAD + status snapshot), not by a filesystem watcher: no dependency, same result for the purposes of scenario 7.
- Version 0.8.0 (0.7.0 is WP15), migration 0003.

## 2026-10-26 (WP15) — implementation choices (awaiting Micah only if he disagrees)
- UI technology: plain HTML/CSS/ES modules served by FastAPI, no Node build and no third-party code (nothing to patch, nothing remote, strict CSP). English text (Q10).
- WebSocket auth: one-time 30-second ticket bound to the stream path (`POST /api/ws-ticket`); the install token is never put in a URL. Closes T-27.
- How the page gets the token: `scripts\start.ps1` opens `http://127.0.0.1:8765/#token=...`; the page stores it in this browser's localStorage (so several tabs work) and removes it from the address bar. "Lock" forgets it; pasting it works too.
- Untrusted projects: every project/task screen says that workers do not load the project's CLAUDE.md, hooks, MCP or skills until the workspace is trusted, and that trusting lets repo scripts run on the PC.
- Not faked: costs show only recorded ledger rows (estimated vs reconciled). Budget admission, reservations and review/diff screens are explicit "arrives with ..." notes until WP6-WP8/WP10 add their APIs (second UI PR then).
- Browser tests: `tests/ui/e2e.mjs` (Node + Playwright, mock runs only) is run by hand, not in CI.

## 2026-10-26 - WP8 assumptions (made by the build thread under Micah's autonomy; tell the thread to change any)
- The wizard's monthly "global" cap is the monthly allocation; the project cap is **per month** per project; the task cap is for the **whole life** of the task (so retries and resumes share it).
- Prices: seeded from SDK_NOTES section 5, re-checked 2026-10-26; Sonnet 5.5 cache read corrected to 0.20 USD/MTok. Micah can append versions through `PUT /api/budget/prices/{model}`.
- Mock runs are free and unaffected unless a test passes an explicit cap (then they are admitted like real runs).
- Not in WP8: daily budget level, cost-center screen (WP15), automatic Console reconciliation (needs an Admin key, SDK_NOTES section 6), the 90 % checkpoint message to a worker (WP10).


## 2026-10-26 (WP7) — implementation choices (awaiting Micah only if he disagrees)
- The scheduler loop is opt-in by environment (`CPM_SCHEDULER=1`, set by `start.ps1`) so nothing starts workers by surprise in tests or ad-hoc runs. It still never spends: API tasks go through the unchanged live gates and the budget ledger, and a refusal only defers the task.
- Automatic retries default to mock tasks only; API tasks are retried by the scheduler only if Micah turns it on (`retry_billing_modes`), because a retry spends again. Defaults: 2 automatic retries, 30 s delay, stop after 2 identical failures.
- Heavy order: among heavy tasks of equal priority the smaller estimate goes first (Nirandyr rule 27). `heavy_limit` is fixed at 1 (Q8).
- Paused/budget-paused tasks and hand-blocked tasks are never started or promoted by the scheduler.

## 2026-10-26 (WP9) — implementation choices (awaiting Micah only if he disagrees)
- On restart an orphaned RUNNING task becomes FAILED (the scheduler may retry mock tasks within its bounds; API tasks are retried only by a person), or PAUSED when it died inside a non-idempotent step (`git commit` today), so nothing is repeated without a human look. Recovery never starts, resumes or completes work.
- Worktrees found without an active task are reported, not removed, because they may hold the only copy of work (Nirandyr rule 27 spirit).
- A backup of the DB is taken at every start (newest 10 kept, `data/backups/`) and on demand; restore is a stop-the-app command, not a web button.
- Version 0.12.0, no migration.

## 2026-10-26 (WP10) - implementation choices (awaiting Micah only if he disagrees)
- A finished worker run ends in TESTING; the gate (clicked, or automatic with the scheduler) decides. Corrections are bounded (default 2, setting `gate.max_corrections`) and stop early when the same failure repeats.
- Tasks need at least one acceptance criterion to be judged (scenario 15); without one the gate waits instead of passing. A task that changes nothing fails.
- Project tests run only in a trusted workspace. A project with no known test command is judged without tests and the result says "skipped".
- API-billed tasks have no live reviewer yet: they stop at "waiting for your review" and you approve after reading the diff (button on the task page). Mock tasks use the mock reviewer. The live reviewer (cheap model, no tools, diff-only) needs a budget go from Micah.
- Publishing is a button, not automatic: push `claude/*` branch, open the PR with a fine-grained GitHub token kept in the Windows credential store. `INTEGRATED` = pull request opened; CPM never merges.
- PR text always has: files changed, how it was tested, security findings, exploit findings, what still needs the PC (Nirandyr rules 37-40).
- Version 0.13.0, migration 0005.

## 2026-10-26 (WP11) - implementation choices (awaiting Micah only if he disagrees)
- Q6 answered in code: CPM reads the job board (WP6) and now also writes it, only through a docs-only PR per action (claim, release), never in place and never on its own: each is a button (UI confirmation) on the task page. Merging the PR stays Micah's click.
- A claim row is `CPM #<task id>: <title> | cpm | <declared scope> | claimed | <date>`; release marks the same row `released` and keeps it. The claimed set is the task's declared scope.
- The board is edited only if it has a Markdown table with files, status and job columns; otherwise CPM refuses (the real layout is still to verify on the PC).
- Version 0.14.0, migration 0006.

## 2026-10-26 (WP12) - implementation choices (awaiting Micah only if he disagrees)
- A lane is configuration in the project profile, not code: command allowlist, resources, validators, artifact policy and report format. WP13 (voice) and WP14 (3D, map, Unity) add profile entries and register validators; they do not change the engine.
- A lane's allowed commands are exact: program name, fixed prefix, listed flags, path arguments confined to the worktree (outputs also to the lane's asset paths). Shells, network tools, deletion tools and git can never be allowed; interpreters only for one fixed script.
- Binary outputs (audio, models, textures) default to `ask`: the gate stops and tells you which files need your decision (Nirandyr rules on binaries). `lfs_only` accepts them when `.gitattributes` routes them to Git LFS; `deny` refuses them. CPM never edits `.gitattributes` or installs LFS.
- A lane whose tool is missing on the PC is deferred (like a refused budget), not failed. A lane check that cannot run on this PC makes the gate wait instead of passing.
- Version 0.15.0, migration 0007. No real API or GitHub call was made.

## 2026-10-26 (WP13) - implementation choices (awaiting Micah only if he disagrees)
- The voice lane is a profile entry (`lanes.voice` in `profiles/nirandyr.yaml`) plus four validators; the engine gained one generic field, `queue_bias`, so "voice is the lowest priority" (Nirandyr rule 49) is a scheduler rule, not a special case.
- Only two commands are allowed: `python tools/voice/render_fixed.py [--check]`, exact, trusted workspace only. No `fetch_kokoro.sh`, `story_qc.py` or `make_ab.py` until they are reviewed and their arguments are known.
- "Restart when the GPU is free" = a READY voice task waits behind the locks and starts when they clear, and the render itself resumes. A FAILED API-billed voice task is not auto-restarted (WP9 decision: no repeated spending without a person); the existing scheduler setting `retry_billing_modes` can include `api` if Micah wants it.
- Loudness is gated RMS + sample peak in dBFS, not LUFS (no extra dependency). `.rva` is not decoded.
- Version 0.16.0, no migration. No real API or GitHub call was made.
