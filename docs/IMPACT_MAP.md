# IMPACT_MAP (skeleton, WP0, v0.0.1, 2026-10-21)

Rule: before editing a module, read its row; after editing, update the row, the chains and the change log (CLAUDE.md rules 2 and 22). Modules do not exist yet; rows describe the planned design (ARCHITECTURE.md).

## 1. Matrix (module -> touches)
| Module (planned) | Reads/writes | Touches (must re-check when changed) | WP |
|---|---|---|---|
| `db` (models, migrations) | SQLite | every module; migrations; backup/restore; security tests on schema | WP1 |
| `security` (token, Origin/CSRF, path validator, secrets, redaction) | keyring, logs | api, workers (env), git, discovery, all tests/security | WP1-2 |
| `api` (FastAPI, WebSocket) | db, scheduler | UI, security middleware, event stream | WP1+ |
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

## 3. Open risks (see THREATS.md for the full list)
High: T-01, T-02, T-03, T-04, T-05, T-06, T-12, T-13, T-18, T-19, T-20 (needs Micah's decision). Process: no `main` branch on the GitHub repo; subscription use of the SDK unverified (CLI mode stays a stub); Admin cost API availability unknown.

## 4. Change log
| Date | Version | Change | By |
|---|---|---|---|
| 2026-10-21 | 0.0.1 | WP0: SDK_NOTES, ARCHITECTURE, THREATS, this skeleton, scripts/check_env.ps1, DECISIONS synced with the planning branch | CPM Plan thread |
