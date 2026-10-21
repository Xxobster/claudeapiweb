# PLAN.md: Claude Project Manager (CPM), action plan for Sonnet implementation threads

Status: DRAFT v1, 2026-10-21, written by the planning thread. Waiting for Micah's answers to section 1 (open questions). The recommended answer is used until he decides.
Spec: `docs/MASTER_PROMPT.md` (intent). Rules: `CLAUDE.md`. Where this plan differs from the master prompt, this plan wins and says why.

## 0. Résumé pour Micah (français)

**Le but réel:** que Claude Desktop reste ton travail principal sur Nirandyr, pendant que des « workers » Claude API (Agent SDK) font en parallèle des travaux séparés: génération de voix, de modèles 3D, de cartes, tests Unity qui vérifient les sorties 3D, etc., sans jamais toucher à ton dossier de travail principal ni se marcher dessus.

**Idée clé du plan:** CPM ne remplace pas ton flux Nirandyr, il s'y branche. Il lit le CLAUDE.md de Nirandyr, respecte ses verrous existants (job board, `devlock.py`, `BUILD_RUNNING.lock`, GPU lock, règle « PR seulement, jamais main »), fait travailler chaque worker dans un worktree séparé, et rend son travail sous forme de branche + PR que le thread Compilation de Nirandyr merge comme d'habitude.

**Trois choses à savoir tout de suite:**
1. Claude (API) ne génère pas lui-même de la voix, des modèles 3D ou des cartes: c'est un modèle texte. Ses workers *pilotent* des outils locaux (TTS local, Blender, scripts de génération de cartes, Unity en ligne de commande) et *vérifient* les résultats. Ça colle à ta règle « gratuit d'abord, GPU sur ton PC ». Tout service payant (voix/3D en ligne) reste exclu sauf ton accord explicite.
2. Le code de l'application doit vivre dans `D:\projects\ClaudeProjectManager`, pas dans OneDrive (la base SQLite en mode WAL et OneDrive ne font pas bon ménage; OneDrive synchronise aussi les venv). Ce dossier OneDrive ne garde que les documents. Le dépôt GitHub est `Xxobster/claudeapiweb` (le nom diffère du nom de l'app, ce n'est pas grave).
3. L'API est facturée à l'usage, séparément de ton abonnement Claude Desktop, ce qui touche ta règle « jamais de service payant » (la règle 50 de Nirandyr l'autorise avec ton go pour un budget payant). Il me faut ta décision (Q1).

**Le projet du master prompt est énorme** (7 phases). Je recommande de le livrer en tranches verticales utilisables dès la première (section 4): une première version qui sait déjà lancer UN worker en sécurité sur UN projet avec budget, puis on ajoute le parallélisme, les verrous, puis les « lanes » voix/3D/maps/Unity.

Questions ouvertes: voir section 1.

---

## 1. Open questions for Micah (each with a recommendation)

| # | Question | Recommendation |
|---|---|---|
| Q1 | Budget API: do you give a go for Anthropic API spending (Nirandyr rules 36/50 say no paid service without your go)? What monthly cap and what small validation budget for first live tests (e.g. 2 USD)? | Yes with a hard monthly cap you choose; mocks for everything until the first live smoke test of 2 USD. Haiku/Sonnet by default, Opus only on demand. |
| Q2 | Code location: `D:\projects\ClaudeProjectManager` cloned from `Xxobster/claudeapiweb`, OneDrive folder = docs only? | Yes. If you prefer the OneDrive folder for code, exclude `.venv`, `data/` and `.git` from sync and keep the DB at `%LOCALAPPDATA%\CPM` (still worse). |
| Q3 | Which media tools exist on your PC today for each lane: voice (which local TTS/training?), 3D (Blender version and script entry points, any local AI 3D model?), maps (existing generator in `tools/`?), Unity version and test runner (Edit Mode via `-runTests`)? | Give me paths/commands (or let the Nirandyr thread list them); I only implement lanes for tools that exist. |
| Q4 | Is a third-party paid API (voice, 3D) wanted at all? | No in v1 (provider abstraction only). |
| Q5 | Integration policy for Nirandyr workers: branch + PR to `main` (rules 37-40), merged only by Compilation? | Yes, CPM never merges into Nirandyr `main`. CPM needs a GitHub token (fine-grained, PRs + contents on `Xxobster/Nirandyr` only) stored in Windows Credential Manager. OK? |
| Q6 | Job board: should CPM only READ `docs/JOB_BOARD.md` to learn Desktop claims (v1), or also write claim rows (through a docs-only PR, rule 40)? | Read-only in v1; write via docs-only PR in a later package (WP11). |
| Q7 | Is the Nirandyr checkout used by Desktop `D:\projects\game` also the repo where workers get worktrees (worktrees under `D:\projects\cpm-worktrees\nirandyr\<task>`)? Disk space for a Unity `Library/` per worktree (often 5-30 GB)? | Yes worktrees outside OneDrive; Unity validation lane reuses ONE dedicated validation worktree with a persistent Library, never one per task. |
| Q8 | Default parallelism: 3 coding agents (master prompt). Your PC also runs Unity/Blender/GPU jobs. | 3 for text/code tasks; max 1 heavy task (Unity, Blender, GPU) at a time, enforced as resource locks. |
| Q9 | Subscription-backed CLI mode (Claude Code CLI on your subscription): build the adapter now or leave the interface only? Terms of use for automated use of a subscription must be checked first. | Interface only in v1; API-billed mode is the only live mode. |
| Q10 | UI language: English or French? | English UI, French later via a message catalog. |
| Q11 | Do you want CPM to auto-start with Windows? | No; desktop shortcut + `start.ps1`. |

---

## 2. Findings and anticipated problems (fix in design, not later)

1. **Claude cannot make voice/3D/maps** itself. Lanes = recipes that run local tools (see section 6). The worker's value: writing/adapting generation scripts, running them with correct parameters, validating outputs, retrying within bounds, writing the report.
2. **OneDrive + SQLite WAL + venv** = corruption/sync storms. Code and data outside OneDrive (CLAUDE.md "Locations").
3. **Two truths about "who edits what".** Desktop edits `D:\projects\game` directly; workers must never. Workers use git worktrees + branches. Collision happens at integration (PR into `main`), so: declared file scope per task, a path-prefix reservation table, a Desktop-claims table (read from `docs/JOB_BOARD.md` and manual UI entries), a filesystem watcher plus `git status` snapshots on the main tree, and mandatory conflict revalidation (merge-tree dry run against current `main`) before opening a PR.
4. **Nirandyr already has locking** (job board, `devlock.py` in `%USERPROFILE%/.device-locks`, `BUILD_RUNNING.lock`, `tools/gpu_lock.sh`). CPM must use them, not invent a rival. CPM's SQLite reservations cover CPM-internal exclusion; "external lock adapters" bridge to the Nirandyr mechanisms (section 5).
5. **Unity specifics:** only one Unity instance can open a project folder; Library import of a fresh worktree is very slow; `.meta` files and GUIDs must travel with their assets; Unity batch needs the Editor closed on that folder. Hence one persistent validation worktree, `unity_editor` as an exclusive resource, `.meta` files reserved with their asset.
6. **Heavy resource contention:** GPU (voice, Blender), Unity, builds. The scheduler treats them as typed exclusive resources, with Nirandyr's priority order (builds > voice > art; long jobs yield to short ones, rule 27) and refuses to start heavy work while `BUILD_RUNNING.lock` exists.
7. **Agent SDK facts must be verified, not assumed:** effort levels (low..max), `max_budget_usd`, permission modes, session resume, hooks, model IDs, how usage/cost is reported, whether it runs cleanly in a separate process on Windows, auth precedence (an `ANTHROPIC_API_KEY` in the environment can silently switch billing from a subscription login to API billing; CPM must set the child environment explicitly). WP0 produces `docs/SDK_NOTES.md` before any dependent code.
8. **Untrusted project instructions:** a managed repo's CLAUDE.md is guidance, never authorization. Hooks/MCP/scripts of a managed repo are off until the user marks the workspace trusted in the UI.
9. **Budget race:** admission control = a SQLite transaction that reserves estimated spend (`BEGIN IMMEDIATE`), reconciled with actual usage at the end; per-run `max_budget_usd` is only a soft backstop.
10. **Scope size:** the master prompt is months of work. Vertical slices, section 4.
11. **OneDrive path with spaces** (`20 - PROJECTS`): always pass paths as structured args; test with spaces and non-ASCII.
12. **CRLF/LF and Windows paths:** normalize in reservations (case-insensitive, `\`→`/`, resolve `..` and junctions) or two tasks collide unseen.
13. **Local web server security:** 127.0.0.1 only, random per-install token + Origin/Host checks (DNS rebinding), no CORS, CSRF token on mutations.

---

## 3. Target architecture (decisions, all revisable via DECISIONS.md)

- Repo layout (`D:\projects\ClaudeProjectManager`):
  `src/cpm/{api,core,db,scheduler,workers,git,locks,budget,discovery,providers,lanes,security}/`, `src/cpm/web/` (static TS/HTML, no heavy framework; Alpine/HTMX-level or plain TS with esbuild-free ES modules), `migrations/` (Alembic), `tests/{unit,integration,e2e,security}/`, `scripts/{setup.ps1,start.ps1,backup.ps1}`, `profiles/` (per-project profiles, e.g. `nirandyr.yaml`), `docs/`.
- Python 3.12, FastAPI, Uvicorn, SQLAlchemy 2 + Alembic, SQLite WAL + `foreign_keys=ON` + `busy_timeout`, pytest, Playwright, `claude-agent-sdk`, `keyring` (Windows Credential Manager) for secrets (DPAPI fallback), `psutil` for process trees.
- **Process model:** backend = supervisor. Each task = `python -m cpm.workers.run --task <id>` as a child process in a Windows Job Object (kill-on-close, kills whole tree), env built explicitly (no inherited secrets except the one key, passed via env to that child only), stdout JSONL to a per-run log file, heartbeat rows every N s, structured result JSON persisted. PowerShell is a supported launcher/inspector (`pwsh -NoProfile -File`) but Python `subprocess` with arg lists is the default.
- **Provider abstraction:** `Provider` protocol (`run_task(spec) -> events`), implementations: `MockProvider` (default in tests, scripted usage/cost), `AgentSdkProvider` (API-billed), `ClaudeCliProvider` (stub, disabled).
- **Scheduler:** deterministic loop (single asyncio task in backend): ready-queue by priority → conflict check against reservations/Desktop claims/external locks → budget admission → spawn worker. All state transitions in one `transition()` function with an allowed-transition table; every transition logged to an `events` table.
- **Task acceptance gate:** deterministic. A task reaches COMPLETED only when: worker exited 0 with a structured result, every acceptance criterion has a verified status (test command exit 0, file-exists check, or reviewer verdict), actual changed files ⊆ declared scope (else CORRECTING or human review), no secrets in diff. INTEGRATED only after PR opened/merge done per policy.
- **Git policy engine:** per-project config: protected branches, integration mode (`pr_only` for Nirandyr, `local_merge` for toy repos), branch naming `claude/<role>-<task>`. Never force/reset/clean; merge-tree dry run; refuse if main tree has unrelated dirty files in the task's scope.
- **Reservations:** table `reservations(resource_type, resource_key_normalized, task_id, mode[excl|shared], lease_expires_at, heartbeat_at)`; acquisition of a task's full set in ONE `BEGIN IMMEDIATE` transaction; prefix-overlap semantics (a directory lock conflicts with any file under it); lease renewal by worker heartbeat; expiry only after process-liveness check.
- **External lock adapters** (`src/cpm/locks/adapters/`): `job_board` (parse `docs/JOB_BOARD.md`, read-only), `devlock` (call `python tools/devices/devlock.py` with structured args), `build_lock` (watch `BUILD_RUNNING.lock`), `gpu_lock` (`tools/gpu_lock.sh` via Git Bash), `desktop_claim` (manual UI claim: "Desktop is working on these paths", with optional expiry).
- **Cost ledger:** append-only `usage_events` (dedup key = SDK message id), `Decimal` USD stored as integer micro-dollars; price table in DB versioned and editable (verify prices from official docs, WP0); budgets at run/task/project/day/month; 70%/90%/100% behaviour per master prompt section 12; `PAUSED_BUDGET` preserves worktree.

---

## 4. Delivery order: vertical slices (replaces the 7 horizontal phases)

Rationale: Micah wants parallel Desktop + API work early. Slice 1 already gives one safe worker; later slices add parallelism, lanes, polish. Each work package (WP) = one Sonnet thread = one PR. Opus does WP0 design review and a review at the end of each slice.

| Slice | Contains | Result Micah can use |
|---|---|---|
| S1 "One safe worker" | WP0-WP5 | Register Nirandyr read-only, see its CLAUDE.md/TODO/memory sources, create a task, run ONE mock/live worker in a worktree with budget cap and live log; result as branch. |
| S2 "Parallel without collisions" | WP6-WP8 | Several workers, reservations, Desktop claims, external lock adapters, crash recovery, global budget admission. |
| S3 "Verified output" | WP9-WP10 | Test/review/correction loop, acceptance gate, PR integration for Nirandyr, diff review UI. |
| S4 "Nirandyr lanes" | WP11-WP14 | Voice, 3D, maps, Unity validation lanes, using local tools. |
| S5 "Hardening" | WP15-WP16 | Full dashboard polish, security suite, docs, installers, final test report. |

### Work packages

Each WP lists: goal, key files, acceptance (testable), tests, model/effort, depends on. All WPs include docs updates (rule 2) and pass `ruff` + `mypy --strict` on new code + pytest.

**WP0: Verification and design lock** (Opus high; no product code). Deliver `docs/SDK_NOTES.md` (verified, dated, with links: Agent SDK Python install/auth/permissions/hooks/sessions/budget/effort/usage fields, model IDs and prices, Console usage/cost API limits, Windows specifics); environment check script `scripts/check_env.ps1` output recorded (Python 3.12, git, pwsh, node, Claude Code, Unity/Blender paths); `docs/ARCHITECTURE.md` (sections 3 of this plan refined), `docs/THREATS.md` (security/concurrency/budget risk list before code), `docs/DECISIONS.md` (Micah's Q answers), `docs/IMPACT_MAP.md` skeleton. Acceptance: every SDK claim in later WPs cites SDK_NOTES.

**WP1: Skeleton** (Sonnet medium). Repo init, `pyproject.toml`, FastAPI app on `127.0.0.1:8765`, config loader, structured logging with secret redaction filter, SQLAlchemy models for ALL core tables (projects, tasks, task_deps, runs, events, reservations, usage_events, budgets, project_sources, settings, prompts), Alembic baseline, WAL pragmas, `scripts/setup.ps1` + `start.ps1` (venv, deps, open browser), CI workflow (pytest + ruff). Acceptance: `start.ps1` serves `/health`; migrations up/down; DB pragmas asserted by test; redaction test (a fake key never appears in logs). Parallel with: none.

**WP2: Security core and credentials** (Sonnet high + Opus review). Local token + Origin/Host/CSRF middleware; secret store (keyring/DPAPI) with save/replace/test/remove, key never returned to frontend; first-run wizard API (key validation via MockProvider unless a live flag + Micah go); path validator (approved roots, no traversal/junction escape, spaces/Unicode). Tests: `tests/security/` (traversal, injection, key exposure, CSRF, DNS rebinding Host header). Depends on WP1.

**WP3: Project registry and discovery** (Sonnet medium). Add project (path + auto-detect git remote, else manual), read-only adapters; discovery of CLAUDE.md, CLAUDE.local.md, AGENTS.md, MEMORY.md, `.claude/{rules,skills,settings.json}`, docs/TODO sources (configurable globs, e.g. Nirandyr `docs/JOB_BOARD.md`, `docs/COORDINATOR_INBOX.md`, `docs/plans/`), git status/branches/remotes, build/test commands detection; include/exclude/refresh UI API; workspace-trust flag (hooks/MCP/scripts off until trusted); Claude's native instruction loading is used (worker `cwd` = worktree and `setting_sources` per SDK_NOTES), no duplication of CLAUDE.md into prompts. Acceptance scenarios 1-3 of the master prompt (local repo; manual remote; CLAUDE.md + memory detection) using fixture repos in `tests/fixtures/`. Depends on WP1, WP2.

**WP4: Tasks, DAG, state machine** (Sonnet medium). Task CRUD, statuses (all 14), allowed-transition table, dependency DAG with cycle rejection (set-based SQL / iterative DFS), ready-queue query, split/merge/edit/reprioritize/reassign/cancel, TODO import (deterministic parser first; AI planner stub via MockProvider, Haiku medium when live), prompt manager tables. Acceptance: scenario 4 (TODO → tasks), cycle rejection, illegal transitions rejected, "no task silently completed" invariant test (scenario 20, partial). Depends on WP1.

**WP5: Worker runner and live monitoring** (Sonnet high; Opus review of process/budget safety). `workers/run.py` child process, Windows Job Object tree-kill, heartbeat, JSONL logs, WebSocket live stream, stop/pause/resume (session resume via SDK), `MockProvider` scripted scenarios (success, fail, hang, crash, budget burst), `AgentSdkProvider` behind explicit API-mode flag, per-task model+effort, worktree creation (`git worktree add` with structured args under `D:\projects\cpm-worktrees\<project>\<task>`), cleanup rules (never delete worktrees with changes). Acceptance: scenarios 12 and 19 (crash recovery of one worker; cancel kills child tree), one live run only after Micah approves the validation budget. Depends on WP1-WP4.

**WP6: Reservations and external locks** (Sonnet high + Opus review). Reservations table logic, atomic multi-resource acquisition, prefix semantics, lease/heartbeat/expiry, normalization (case, slashes, `..`, `.meta` pairing for Unity), lock adapters (section 3), Desktop claims UI+API, conflict explanation in UI ("blocked by: Desktop claim on `Assets/Game/UI/`"). Acceptance: scenarios 6 (conflicting reservations rejected), 7 (external modification detected via watcher/git snapshot), concurrency stress test (50 threads acquiring overlapping sets: no double grant). Depends on WP4.

**WP7: Scheduler** (Sonnet high). Ready-queue → conflict → budget admission → spawn; concurrency limits (global, per project, per resource class: `heavy=1`); timeouts, retry bounds, repeated-identical-failure detection; Nirandyr priority rules for heavy jobs (yield to shorter, refuse while build lock). Acceptance: scenario 5 (two independent tasks in parallel), deterministic scheduling order tests. Depends on WP5, WP6.

**WP8: Budgets and cost center** (Sonnet high; Opus review of ledger). Ledger, dedup, admission reservations (`BEGIN IMMEDIATE`), thresholds 70/90/100, `PAUSED_BUDGET` flow and the five user options, resume keeps cumulative spend, manual credit-balance entry, estimate-vs-reconciled labelling, cost center UI. Acceptance: scenarios 8, 9, 10 (shared global budget across two projects with two concurrent workers, property test: sum of reservations never exceeds cap). Depends on WP5, WP7.

**WP9: Recovery** (Sonnet high). Restart reconciliation (orphan processes, leases, worktrees, budget reservations), no duplicate execution, non-idempotent step markers, backup/restore of DB (`sqlite3 .backup`). Acceptance: scenarios 11, 12, 18. Depends on WP7, WP8.

**WP10: Test/review/correct loop, git integration, diff UI** (Sonnet high; Opus review of gate). Acceptance gate, targeted test runner per project profile, independent reviewer run (cheaper model, fresh context, diff-only input), bounded correction attempts, unexpected-file detection, merge-tree conflict revalidation, `pr_only` integration (push branch + open PR via GitHub API, token from secret store), never merge/push main/force/reset/clean, preserve unrelated local changes. Acceptance: scenarios 13-16, 20. Depends on WP9.

**WP11: Job board writer + claims sync** (Sonnet medium, optional per Q6). Docs-only PR adding/releasing claim rows in `docs/JOB_BOARD.md` per Nirandyr rule 40. Depends on WP10.

**WP12: Lane framework** (Sonnet high). `lanes/` plugin interface: a lane = {task template, required tools/paths, resource set (e.g. `gpu`, `unity_editor`), allowed commands (allowlist), output validators, artifact policy (outputs go in the worktree under the project's asset path; large binaries per the project's git-LFS policy: ASK Nirandyr rules before committing binaries), report format}. Lane config from `profiles/<project>.yaml`. Depends on WP10.

**WP13: Voice lane** (Sonnet medium). Worker drives the existing local voice pipeline (Q3), allowlisted commands only, GPU lock, "restart when GPU free" per Nirandyr rule 49 as a scheduler rule (lowest priority, yields to builds/art), validators: file exists, duration range, loudness, sample rate, no silence, line list completeness vs script. Depends on WP12 + Q3.

**WP14: 3D, map and Unity validation lanes** (Sonnet high; can split into three PRs). 3D: Blender batch scripts, validators (poly count, UV present, scale, naming, `.fbx/.glb` opens); map: existing generator, validators (dimensions, connectivity, seed reproducibility); Unity validation: Edit Mode tests via `Unity -batchmode -runTests` on the dedicated validation worktree, import checks (no missing GUID/meta, no compile errors), results parsed from NUnit XML into the acceptance gate; uses `unity_editor` exclusive resource + `BUILD_RUNNING.lock`/`gpu_lock` adapters. Depends on WP12 + Q3.

**WP15: Dashboard completion** (Sonnet medium). Global/project dashboards, Kanban + table, agent monitor, git/review center, settings, empty/loading/disconnected states, no fake data. Playwright tests across two projects in two tabs. Depends on WP10 (can begin earlier on finished APIs).

**WP16: Hardening and release** (Opus xhigh security review, then Sonnet fixes). Full `tests/security` suite, e2e multi-project scenarios (all 20), docs set (README, ARCHITECTURE, SECURITY, API/SDK, USER_GUIDE, KNOWN_LIMITATIONS, TEST_REPORT), installers, shortcut. Depends on all.

Parallelisable pairs once WP4 exists: WP3 ‖ WP4; WP6 ‖ WP8 groundwork (ledger schema); WP13 ‖ WP14 after WP12; WP15 ‖ anything after the APIs exist (separate files, separate branches).

---

## 5. Nirandyr profile (`profiles/nirandyr.yaml`, written in WP3, extended in WP6/WP12)

```yaml
id: nirandyr
path: D:\projects\game          # read-only for CPM; main tree edited only by Desktop
remote: https://github.com/Xxobster/Nirandyr
worktree_root: D:\projects\cpm-worktrees\nirandyr
integration: pr_only            # rules 37-40; never push main; Compilation merges
protected_branches: [main]
branch_pattern: "claude/{role}-{task_slug}"
instruction_sources: [CLAUDE.md, docs/plans/00_DECISIONS_MICAH.md, docs/plans/99_IMPACT_MAP.md]
task_sources: [docs/JOB_BOARD.md, docs/COORDINATOR_INBOX.md]
external_locks:
  job_board: docs/JOB_BOARD.md      # read-only in v1
  devlock: tools/devices/devlock.py
  build_lock: D:\projects\game-build\BUILD_RUNNING.lock
  gpu_lock: tools/gpu_lock.sh
heavy_resources: [unity_editor, gpu, blender, device]
default_effort: medium
pr_body_must_list: [files_changed, security_findings, exploit_findings, what_needs_pc]
```
Acceptance for the profile: a worker task against Nirandyr cannot start while `BUILD_RUNNING.lock` exists if it is heavy, cannot claim a file listed as claimed on the job board, and its result is a pushed branch + PR text containing the required sections. Needs WP10 for the PR part.

Open check: Nirandyr rules 37-40 describe PR flow by "cloud threads"; CPM workers act like cloud threads from Nirandyr's point of view. Ask the Nirandyr coordinator to acknowledge that CPM branches are valid under rules 37-40 (one line from Micah suffices, Q5).

---

## 6. How Desktop and API workers split the work (default policy)

- **Desktop owns:** the main tree, design, UI, gameplay code, anything touching shared docs, merges and builds.
- **API workers own (lanes):** voice generation and checks, 3D generation and checks, map generation and checks, Unity validation runs, bulk/repetitive data jobs, doc/test drafting on separate paths.
- **Separation mechanism:** declared path scopes per lane (e.g. voice → `Assets/Audio/Voice/**`, 3D → `Assets/Models/**` + `.meta`, maps → `Assets/Maps/**` — exact paths from Q3), reserved in CPM; any overlap with a Desktop claim or job-board claim blocks the task with a visible reason.
- **Handoff:** worker output = branch + PR + report (`docs/jobs/` note per Nirandyr rule 34 when it needs another thread). Micah sees it in CPM's review center and on GitHub.

---

## 7. Test strategy

- Mock-first: `MockProvider` scripted scenarios power all 20 required scenarios; no API key needed for install or tests.
- Pyramid: unit (DAG, transitions, ledger math, path normalization) → integration (SQLite + git fixtures built in tmp dirs, real subprocess workers with MockProvider) → e2e (Playwright, two projects, two tabs) → security.
- Windows-only behaviours (Job Objects, PowerShell, DPAPI) tested with `pytest.mark.windows`; the cloud threads cannot run them, so each WP's PR lists "needs PC run" items and a PC test thread (or Micah's `scripts/run_windows_tests.ps1`) runs them. Cloud-friendly code paths must work on Linux with abstractions (`ProcessTree`, `SecretStore`) so Sonnet can test most logic in the cloud.
- Real API: one smoke test (`tests/live/`, marked, off by default) after Micah's explicit budget go (Q1).

## 8. Definition of done per WP
Code + tests green (state which ran, which need the PC) · IMPACT_MAP, DECISIONS, USER_GUIDE updated · security findings written · PR opened with `CLAUDE.md` rule 5 contents · handoff note · fresh thread recommended for the next WP.

## 9. Recommended next steps
1. Micah answers Q1-Q3 (the others default to the recommendations).
2. Run WP0 (Opus) now; it needs no API spend and unblocks everything.
3. Start WP1 in a Sonnet thread as soon as WP0 merges; WP3 and WP4 in parallel after WP2.
