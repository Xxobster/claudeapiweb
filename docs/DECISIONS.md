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
- Cost estimate per message uses the SDK_NOTES section 5 prices (integers, never under-counts unknown models); since v0.6.0 (WP8) it comes from the DB price table (`budget/pricing.py`). Usage rows dedup on the message id; a final reported total above the sum adds a reconciling row, except for resumed sessions (their total includes earlier runs).
- Workers get no auto-approved tools: every file and Bash call goes through `workers/permissions.py`. Bash = one simple allowlisted command (git read/add/commit, `ls`, `pwd`, and in a trusted workspace `python -m pytest|ruff|mypy`). Lanes (WP12) will add their own allowlists.
- Worktree base is `origin/main` when it exists, otherwise HEAD. Content filters (LFS) are blanked for checkout, so LFS files appear as pointers until WP12 decides the binary policy.
- WebSocket progress is a 0.2 s tail of the JSONL file, so reconnecting with `since=<seq>` replays exactly.

## 2026-10-26 - WP8 assumptions (made by the build thread under Micah's autonomy; tell the thread to change any)
- The wizard's monthly "global" cap is the monthly allocation; the project cap is **per month** per project; the task cap is for the **whole life** of the task (so retries and resumes share it).
- Prices: seeded from SDK_NOTES section 5, re-checked 2026-10-26; Sonnet 5.5 cache read corrected to 0.20 USD/MTok. Micah can append versions through `PUT /api/budget/prices/{model}`.
- Mock runs are free and unaffected unless a test passes an explicit cap (then they are admitted like real runs).
- Not in WP8: daily budget level, cost-center screen (WP15), automatic Console reconciliation (needs an Admin key, SDK_NOTES section 6), the 90 % checkpoint message to a worker (WP10).

