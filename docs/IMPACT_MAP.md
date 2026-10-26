# IMPACT_MAP (WP4, v0.4.0, 2026-10-26)

Rule: before editing a module, read its row; after editing, update the row, the chains and the change log (CLAUDE.md rules 2 and 22). Modules marked (built) exist in code; the others describe the planned design (ARCHITECTURE.md).

## 1. Matrix (module -> touches)
| Module (planned) | Reads/writes | Touches (must re-check when changed) | WP |
|---|---|---|---|
| `db` (built: `src/cpm/db/{engine,models,migrate}.py`, `migrations/`) | SQLite | every module; migrations; backup/restore; security tests on schema | WP1 |
| `security` (built: `redaction.py`, `tokens.py`, `middleware.py`, `secrets_store.py`, `paths.py`, `childenv.py`; `config.py` loopback + no-OneDrive) | keyring, `data/install_token`, logs | api, workers (env), git, discovery, reservations (paths), all tests/security | WP1-2 |
| `providers/keycheck` (built: Mock + HTTP key validators) | Anthropic `/v1/models` only when live flag set | api/credentials, WP5 providers | WP2 |
| `api` (built: `app.py`, `credentials.py`, `wizard.py`, `projects.py`, `tasks.py`) (FastAPI, WebSocket) | db, scheduler | UI, security middleware, event stream | WP1+ |
| `discovery` (built: `discovery/{gitinfo,scan,commands,policy}.py`, `profiles.py`, `api/projects.py`, `profiles/*.yaml`) | managed repo (read-only), `projects`/`project_sources`/`events` tables, `approved_roots` + `project.<id>.patterns` settings | tasks import, worker prompts, workspace trust | WP3 |
| `tasks` (built: `src/cpm/tasks/{states,service,todo_import,planner,prompts}.py`, `api/tasks.py`) | db (`tasks`, `task_deps`, `events`, `prompts`) | scheduler (ready queue, transitions), workers (RUNNING/TESTING...), acceptance gate (COMPLETED evidence), budgets (PAUSED_BUDGET), reservations (`declared_scope`), UI | WP4 |
| `workers` (runner, providers) | worktrees, SDK | budget ledger, reservations, logs, Job Object, billing mode | WP5 |
| `git` (worktrees, policy, PR) | managed repo, GitHub | tasks, security (token), reservations, UI review center | WP5, WP10 |
| `locks` (reservations, adapters) | db, Nirandyr lock files | scheduler, UI conflict messages, Nirandyr scripts (read-only) | WP6 |
| `scheduler` | locks, budget, workers | tasks, UI, resource classes (`heavy=1`) | WP7 |
| `budget` (ledger, admission) | db | scheduler, workers (usage), UI cost center, SDK_NOTES prices | WP8 |
| `recovery` | db, processes, worktrees | scheduler, locks, budget, git | WP9 |
| `lanes` (voice, 3D, map, Unity) | Nirandyr tools | locks (gpu/unity), git, budget, Nirandyr rules | WP12-14 |
| `web` (UI) | api | every API contract | WP15 |
| docs / profiles (`profiles/nirandyr.yaml`) | files | lanes, locks, git | WP3+ |

## 2. Consequence chains (examples to keep true)
- Change a task status rule -> transition table -> scheduler -> UI states -> acceptance-gate tests -> events log.
- Change the child env builder -> billing mode (T-12) -> budget ledger -> security tests (key exposure) -> SDK_NOTES section 2.
- Change path normalization -> reservations (T-24) -> path validator (T-03) -> git scope check -> lane artifact paths.
- Change price table or model IDs -> admission estimates -> budget UI -> SDK_NOTES section 5 (re-verify with the docs, date it).
- Change a lock adapter -> Nirandyr contract (`devlock.py`, `gpu_lock.sh`, `BUILD_RUNNING.lock` formats) -> heavy-task start rule -> lanes.
- Change the Nirandyr profile -> lanes allowlists -> THREATS T-20 decision.

## 2c. WP2 notes
- Every route is behind `SecurityMiddleware` (fail closed). Public: `GET /health` only (Host still checked). A new public route means editing `PUBLIC_PATHS` and a test in `tests/security/test_guard.py`.
- Mutations need `X-CPM-Token` + `X-CPM-CSRF` (CSRF = HMAC of the token, from `GET /api/session`). No CORS middleware may ever be added.
- Any new endpoint taking a path must call `security.paths.resolve_within`/`resolve_in_any` with roots from the `approved_roots` setting. Any new subprocess must use argument lists (`tests/security/test_injection.py` greps for shell use) and `childenv.build_child_env`.
- Pydantic's default 422 echoes the input; `app.py` overrides it. New models carrying secrets must use `SecretStr`.
- The API key exists only in the `SecretStore` (Credential Manager). `settings` table holds no secrets; `wizard.config` stores budgets as micro-USD integers.
- Lockfile: after changing dependencies run `uv pip compile pyproject.toml --extra dev --universal --python-version 3.12 --generate-hashes -o requirements.lock`; CI fails if it is stale.
- WP5 must add `claude-agent-sdk` (pinned) to the lock and use `build_child_env`; WP15 must decide the WebSocket token transport (browsers cannot set headers: subprotocol or short-lived ticket).

## 2d. WP4 notes (tasks)
- `tasks/states.py` is the only transition table. Every status change goes through `TaskService.transition` (compare-and-set UPDATE + `events` row). Never `UPDATE tasks SET status` elsewhere.
- COMPLETED needs `evidence={"acceptance_gate": "passed"}`: only the WP10 gate may pass it. The HTTP API refuses all machine states (`USER_TARGETS` = BACKLOG, READY, BLOCKED, PAUSED, CANCELLED). Invariant tests: `tests/unit/test_task_states.py`.
- Scheduler (WP7) must consume `ready_queue()` and call `transition` RESERVED/RUNNING; workers (WP5) TESTING..; budgets (WP8) PAUSED_BUDGET -> READY/RUNNING; recovery (WP9) FAILED/PAUSED -> READY (increments `retry_count`).
- `declared_scope` entries are validated with `security.paths.validate_relative` (syntactic) and are only candidates: WP6 must normalize them again before reserving.
- Graph mutations (dependency add, split, merge) are serialized with a process-wide lock; fine for the single-process server, revisit if CPM ever runs several processes.
- TODO text and planner output are untrusted data (T-05): they become titles/descriptions only. WP5 must quote them as data in worker prompts.
- Prompt store refuses bodies matching the secret redaction patterns.
- Migration `0002` adds `parent_id`, `retry_count`, `source`, `source_ref` (unique per project: import dedup), `started_at`, `completed_at`; initial status is BACKLOG (was DRAFT). Backup/restore (WP9) must include them.
- A new endpoint on the task API needs a test in `tests/security/test_tasks_security.py` (auth + CSRF).

## 3. Open risks (see THREATS.md for the full list)
High: T-01, T-02, T-03, T-04, T-05, T-06, T-12, T-13, T-18, T-19, T-20 (needs Micah's decision). Process: subscription use of the SDK unverified (CLI mode stays a stub); Admin cost API availability unknown.

## 2d. WP3 notes
- Registering a project is the user's explicit grant of that folder: `POST /api/projects` validates the path (`validate_root`), refuses overlap with the data dir and adds the real path to `approved_roots`. Removal only deletes the DB rows; nothing on disk is touched. No schema change (v0.3.0 reuses `projects`/`project_sources`; user globs live in `settings` key `project.<id>.patterns`).
- All git calls go through `discovery/gitinfo._git`: argument lists, `-c core.fsmonitor=false -c core.hooksPath=<null>` plus blanked `filter.*.clean|smudge|process` commands (status runs them; tested), no prompts, no network command, minimal env (no tokens). Credentials embedded in remote URLs are stripped before storing/returning. Never add a git call that can reach the network or run a repo-defined program.
- Discovery hashes files, never returns contents (T-05). A source that resolves outside the root (symlink/junction) is dropped. Globs are checked by `scan.check_pattern`.
- Workspace trust: `worker_policy(trusted)` (`discovery/policy.py`) is the single source for the runner (WP5): untrusted -> `setting_sources=()`, no hooks/MCP/scripts; trusted -> `("project",)`. `user` is never loaded. Consequence: untrusted workers do not load the project's CLAUDE.md natively; the UI (WP15) must tell the user to trust the workspace first. Files that can carry hooks/MCP (`.claude/settings*.json`, `.claude/skills/*`, `.mcp.json`) report `requires_trust` and `active=false` until trusted.
- Command detection only reads manifests and returns argv lists (`npm run test`, `python -m pytest`, ...); script bodies are never reported or run. WP10 must still run them through the acceptance gate with allowlists.
- Profiles (`profiles/<name>.yaml`) are loaded with `yaml.safe_load` into a strict pydantic model (`extra=forbid`); name regex blocks traversal. Profile `path` is only a suggestion. New dependency: `pyyaml` (+ `types-PyYAML` for mypy); lockfile regenerated.
- WP4 (tasks) can read `project_sources` rows of kind `task` for TODO import; WP6 reads `external_locks` from the profile; WP5 reads `worker_policy`.

## 2b. WP1 notes
- Adding/changing a column: edit `models.py`, generate a new Alembic revision (never edit `0001`), re-run `tests/integration/test_health_and_db.py`, update backup/restore (WP9) and the cost ledger (WP8) if money or usage columns change.
- Money columns are integer micro-dollars (`*_microusd`); convert with `Decimal` at the edges only.
- Status columns are free strings until WP4 adds the transition table; do not rely on SQL to validate them.
- Log handlers must use `RedactingFormatter` (`logging_setup.py`); any new handler bypassing it is a T-01 regression.
- `config.load_settings` is the only place reading `CPM_*` env vars.

## 4. Change log
| Date | Version | Change | By |
|---|---|---|---|
| 2026-10-26 | 0.3.0 | WP3: project registry API (`/api/projects`, `/api/profiles`), read-only git inspection, instruction/memory/task source discovery with include/exclude/refresh and custom globs, command detection, workspace-trust flag + `worker_policy`, `profiles/nirandyr.yaml`, fixture-repo tests for acceptance scenarios 1-3, `pyyaml` dependency | CPM Code thread |
| 2026-10-21 | 0.0.1 | WP0: SDK_NOTES, ARCHITECTURE, THREATS, this skeleton, scripts/check_env.ps1, DECISIONS synced with the planning branch | CPM Plan thread |
| 2026-10-24 | 0.2.0 | WP2: install token + Host/Origin/CSRF guard (HTTP and WebSocket), keyring secret store, key wizard/credentials API (Mock validator, gated live check), path validator, explicit child env, hash-locked dependencies, mypy strict, pip-audit in CI, `tests/security/` (traversal, injection, key exposure, CSRF, DNS rebinding) | CPM Code thread |
| 2026-10-21 | 0.1.0 | WP1: skeleton (FastAPI `/health`, SQLAlchemy models for 11 core tables, Alembic baseline `0001`, WAL/FK/busy_timeout pragmas, redacting logger, loopback + no-OneDrive config checks, setup/start scripts, CI) | CPM Code thread |
| 2026-10-26 | 0.4.0 | WP4: task state machine (14 statuses, transition table, no silent completion), dependency DAG with cycle rejection, ready queue, edit/reprioritize/reassign/cancel, split/merge, deterministic TODO import (idempotent), mock AI planner stub, versioned prompt store, `/api/projects/{id}/tasks` and `/api/tasks`, `/api/prompts`; migration 0002 (touches `db/models`, `security/paths.validate_relative`, `api/app`) | CPM Code thread (WP4) |
| 2026-10-26 | 0.2.1 | Security review of WP1/WP2: root-of-disk refused as approved root, git_url credentials rejected, response hardening headers, API docs disabled, extra reserved names, token-file recovery, CI read-only token (touches `security/middleware`, `security/paths`, `api/wizard`, `api/app`, `providers/keycheck`; WP3 project registry must use `validate_root`, which now refuses drive roots) | CPM Audit thread |
