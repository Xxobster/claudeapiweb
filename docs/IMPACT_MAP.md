# IMPACT_MAP (WP2, v0.2.1, 2026-10-26)

Rule: before editing a module, read its row; after editing, update the row, the chains and the change log (CLAUDE.md rules 2 and 22). Modules marked (built) exist in code; the others describe the planned design (ARCHITECTURE.md).

## 1. Matrix (module -> touches)
| Module (planned) | Reads/writes | Touches (must re-check when changed) | WP |
|---|---|---|---|
| `db` (built: `src/cpm/db/{engine,models,migrate}.py`, `migrations/`) | SQLite | every module; migrations; backup/restore; security tests on schema | WP1 |
| `security` (built: `redaction.py`, `tokens.py`, `middleware.py`, `secrets_store.py`, `paths.py`, `childenv.py`; `config.py` loopback + no-OneDrive) | keyring, `data/install_token`, logs | api, workers (env), git, discovery, reservations (paths), all tests/security | WP1-2 |
| `providers/keycheck` (built: Mock + HTTP key validators) | Anthropic `/v1/models` only when live flag set | api/credentials, WP5 providers | WP2 |
| `api` (built: `app.py`, `credentials.py`, `wizard.py`) (FastAPI, WebSocket) | db, scheduler | UI, security middleware, event stream | WP1+ |
| `discovery` | managed repo (read-only) | tasks import, worker prompts, workspace trust | WP3 |
| `tasks` (DAG, state machine) | db | scheduler, UI, acceptance gate, budgets | WP4 |
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

## 3. Open risks (see THREATS.md for the full list)
High: T-01, T-02, T-03, T-04, T-05, T-06, T-12, T-13, T-18, T-19, T-20 (needs Micah's decision). Process: subscription use of the SDK unverified (CLI mode stays a stub); Admin cost API availability unknown.

## 2b. WP1 notes
- Adding/changing a column: edit `models.py`, generate a new Alembic revision (never edit `0001`), re-run `tests/integration/test_health_and_db.py`, update backup/restore (WP9) and the cost ledger (WP8) if money or usage columns change.
- Money columns are integer micro-dollars (`*_microusd`); convert with `Decimal` at the edges only.
- Status columns are free strings until WP4 adds the transition table; do not rely on SQL to validate them.
- Log handlers must use `RedactingFormatter` (`logging_setup.py`); any new handler bypassing it is a T-01 regression.
- `config.load_settings` is the only place reading `CPM_*` env vars.

## 4. Change log
| Date | Version | Change | By |
|---|---|---|---|
| 2026-10-21 | 0.0.1 | WP0: SDK_NOTES, ARCHITECTURE, THREATS, this skeleton, scripts/check_env.ps1, DECISIONS synced with the planning branch | CPM Plan thread |
| 2026-10-24 | 0.2.0 | WP2: install token + Host/Origin/CSRF guard (HTTP and WebSocket), keyring secret store, key wizard/credentials API (Mock validator, gated live check), path validator, explicit child env, hash-locked dependencies, mypy strict, pip-audit in CI, `tests/security/` (traversal, injection, key exposure, CSRF, DNS rebinding) | CPM Code thread |
| 2026-10-21 | 0.1.0 | WP1: skeleton (FastAPI `/health`, SQLAlchemy models for 11 core tables, Alembic baseline `0001`, WAL/FK/busy_timeout pragmas, redacting logger, loopback + no-OneDrive config checks, setup/start scripts, CI) | CPM Code thread |
| 2026-10-26 | 0.2.1 | Security review of WP1/WP2: root-of-disk refused as approved root, git_url credentials rejected, response hardening headers, API docs disabled, extra reserved names, token-file recovery, CI read-only token (touches `security/middleware`, `security/paths`, `api/wizard`, `api/app`, `providers/keycheck`; WP3 project registry must use `validate_root`, which now refuses drive roots) | CPM Audit thread |
