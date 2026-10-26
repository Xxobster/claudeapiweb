# TEST REPORT (v0.18.0, 2026-10-26, WP16)

Run in the cloud container (Linux, Python 3.13, Chromium). No real API call, no GitHub call, no spending. Windows-only tests (`-m windows`) did not run: they need the PC.

| Check | Result |
|---|---|
| `ruff check .` and `ruff format --check .` | clean (199 files) |
| `mypy --strict src` | no issues (98 files) |
| `pytest -m "not windows"` | 857 passed, 1 deselected |
| `node tests/ui/e2e.mjs` (Playwright, mock runs) | all checks passed |
| `pip-audit`, lockfile freshness | run by CI only (not run here) |

## The 20 required scenarios (MASTER_PROMPT section 17)
`tests/e2e/test_scenario_matrix.py` fails if any of these tests is renamed or removed.

| # | Scenario | Proof |
|---|---|---|
| 1 | Local git project | `tests/integration/test_projects.py::test_scenario1_local_git_repo` |
| 2 | Manual remote | `...::test_scenario2_manual_remote` |
| 3 | CLAUDE.md and memory detected | `...::test_scenario3_claude_md_and_memory` |
| 4 | TODO to tasks | `tests/integration/test_tasks_api.py::test_scenario_4_todo_to_tasks_via_api` |
| 5 | Two tasks in parallel | `tests/scheduler/test_scheduler.py::test_scenario_5_...`; two projects: `tests/e2e/test_multiproject.py` |
| 6 | Conflicting reservations rejected | `tests/locks/test_reservations.py::test_conflicting_path_reservations_rejected` (+ 50-thread stress) |
| 7 | External modification detected | `tests/locks/test_external.py::test_external_modification_of_reserved_path_is_detected` |
| 8 | Pause at task budget | `tests/workers/test_budget_runs.py::test_scenario_8_...` |
| 9 | Resume keeps cumulative spend | `...::test_scenario_9_...` |
| 10 | Shared global budget, two projects | `...::test_scenario_10_...` |
| 11 | Stop and restart during a task | `tests/recovery/test_restart.py::test_scenario_11_...` |
| 12 | Recover after a worker crash | `tests/recovery/test_reconcile.py::test_scenario_12_...` |
| 13 | Unsafe git integration refused | `tests/review/test_integrate.py::test_guard_push_refuses_unsafe_branches` |
| 14 | Failed tests, bounded correction | `tests/review/test_gate.py::test_failed_tests_start_a_bounded_correction_that_can_succeed` |
| 15 | Incomplete acceptance criteria | `...::test_task_without_acceptance_criteria_is_not_judged` |
| 16 | Unrelated local changes preserved | `tests/review/test_integrate.py::test_unrelated_local_changes_in_the_main_tree_survive_the_whole_flow` |
| 17 | Secrets kept from frontend and logs | `tests/security/test_key_exposure.py`, `test_hardening.py::test_frontend_has_no_key_material` |
| 18 | State restored after restart | `tests/recovery/test_restart.py::test_scenario_18_...` |
| 19 | Cancel kills the process tree | `tests/workers/test_runner.py::test_cancel_kills_the_whole_process_tree` |
| 20 | No silent success | `tests/review/test_gate.py::test_no_changes_is_a_failure_not_a_success`, `tests/unit/test_task_states.py`, `tests/e2e/test_multiproject.py` |

## Lanes (not part of the 20 scenarios)
Voice (`tests/lanes/test_voice.py`, 44 tests), 3D/map/Unity (`tests/lanes/test_wp14_*.py`) and the lane framework run on fixtures only; their real tools are untested (KNOWN_LIMITATIONS 3 and 5).

## Not tested here (needs the PC)
See `docs/KNOWN_LIMITATIONS.md` section 1: live API run, Windows Job Objects, Credential Manager, PowerShell scripts and shortcut, real GitHub PRs, real Nirandyr tools (Blender, Unity, voice).

## Reproduce
`pip install --require-hashes -r requirements.lock && pip install --no-deps -e . && ruff check . && mypy --strict src && pytest -m "not windows"`; UI: `node tests/ui/e2e.mjs` from the repo root (Playwright, dev venv on PATH).
