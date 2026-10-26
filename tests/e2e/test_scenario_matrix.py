"""WP16: the 20 required scenarios (MASTER_PROMPT section 17) must each map to a real test.

The matrix is the single list of which test proves which scenario. If a test is renamed or removed,
this file fails, so a scenario can never lose its proof silently (docs/TEST_REPORT.md mirrors it).
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

MATRIX: dict[int, list[str]] = {
    1: ["tests/integration/test_projects.py::test_scenario1_local_git_repo"],
    2: ["tests/integration/test_projects.py::test_scenario2_manual_remote"],
    3: ["tests/integration/test_projects.py::test_scenario3_claude_md_and_memory"],
    4: ["tests/integration/test_tasks_api.py::test_scenario_4_todo_to_tasks_via_api"],
    5: ["tests/scheduler/test_scheduler.py::test_scenario_5_two_independent_tasks_run_in_parallel"],
    6: ["tests/locks/test_reservations.py::test_conflicting_path_reservations_rejected"],
    7: ["tests/locks/test_external.py::test_external_modification_of_reserved_path_is_detected"],
    8: [
        "tests/workers/test_budget_runs.py"
        "::test_scenario_8_task_budget_exhausted_pauses_and_releases_the_hold"
    ],
    9: [
        "tests/workers/test_budget_runs.py::test_scenario_9_resume_does_not_reset_cumulative_spend"
    ],
    10: [
        "tests/workers/test_budget_runs.py::test_scenario_10_two_projects_share_one_global_budget"
    ],
    11: ["tests/recovery/test_restart.py::test_scenario_11_stop_during_a_task_then_restart"],
    12: [
        "tests/recovery/test_reconcile.py::test_scenario_12_orphaned_running_task_is_recovered_once"
    ],
    13: ["tests/review/test_integrate.py::test_guard_push_refuses_unsafe_branches"],
    14: [
        "tests/review/test_gate.py::test_failed_tests_start_a_bounded_correction_that_can_succeed"
    ],
    15: ["tests/review/test_gate.py::test_task_without_acceptance_criteria_is_not_judged"],
    16: [
        "tests/review/test_integrate.py"
        "::test_unrelated_local_changes_in_the_main_tree_survive_the_whole_flow"
    ],
    17: [
        "tests/security/test_key_exposure.py::test_not_in_logs_db_or_data_dir",
        "tests/security/test_hardening.py::test_frontend_has_no_key_material",
    ],
    18: [
        "tests/recovery/test_restart.py::test_scenario_18_crash_then_restart_restores_dashboard_and_state"
    ],
    19: ["tests/workers/test_runner.py::test_cancel_kills_the_whole_process_tree"],
    20: [
        "tests/review/test_gate.py::test_no_changes_is_a_failure_not_a_success",
        "tests/unit/test_task_states.py",
        "tests/e2e/test_multiproject.py::test_no_task_is_completed_without_the_gate",
    ],
}


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
    }


def test_matrix_covers_scenarios_1_to_20():
    assert sorted(MATRIX) == list(range(1, 21))


@pytest.mark.parametrize("scenario", sorted(MATRIX))
def test_every_scenario_points_at_an_existing_test(scenario):
    assert MATRIX[scenario], f"scenario {scenario} has no proof"
    for ref in MATRIX[scenario]:
        file_part, _, name = ref.partition("::")
        path = ROOT / file_part
        assert path.is_file(), f"scenario {scenario}: {file_part} is missing"
        if name:
            assert name in _test_names(path), f"scenario {scenario}: {ref} no longer exists"
        else:
            assert _test_names(path), f"scenario {scenario}: {file_part} has no tests"
