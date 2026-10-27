"""Supervisor + real child processes with the MockProvider (no network, no key)."""

import json
import os
import signal
import time

import pytest

from cpm.workers.supervisor import LiveRunNotAllowed, RunError, RunRefused


def _alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill(pid, 0) would TERMINATE the process on Windows: ask for its exit code instead.
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return (
                bool(k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)))
                and code.value == 259
            )
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _events(env, run_id):
    return env.sup.run_log(run_id).read(0, 1000)


def test_success_moves_task_to_testing_never_completed(env):
    t = env.new_task()
    run = env.sup.start(t.id, heartbeat_s=0.2)
    assert run.status == "RUNNING" and run.billing_mode == "mock"
    done = env.sup.wait(run.id)
    assert done.status == "SUCCEEDED" and done.exit_code == 0
    # No acceptance gate yet (WP10): the task waits in TESTING, it is not COMPLETED.
    assert env.tasks.get(t.id).status == "TESTING"
    kinds = [e["type"] for e in _events(env, run.id)]
    assert kinds[0] == "run_created" and "result" in kinds and kinds[-1] == "run_ended"
    assert done.cost_microusd == 1000
    assert (
        env.settings.worktree_root / "demo" / f"task-{t.id}" / f"mock_output_task{t.id}.txt"
    ).exists()
    assert not (env.repo / f"mock_output_task{t.id}.txt").exists()  # main tree untouched


def test_worker_failure_marks_task_failed(env):
    t = env.new_task()
    run = env.sup.wait(env.sup.start(t.id, scenario="fail").id)
    assert run.status == "FAILED"
    assert env.tasks.get(t.id).status == "FAILED"


def test_crash_is_recovered_and_isolated(env):
    """Scenario 12: one worker dies abruptly; the other keeps running and finishes."""
    a, b = env.new_task("crashing"), env.new_task("steady")
    ra = env.sup.start(a.id, scenario="crash")
    rb = env.sup.start(b.id, scenario="slow", scenario_args={"steps": 8, "delay_s": 0.1})
    assert env.sup.wait(ra.id).status == "CRASHED"
    assert env.sup.wait(rb.id).status == "SUCCEEDED"
    assert env.tasks.get(a.id).status == "FAILED" and env.tasks.get(b.id).status == "TESTING"
    # The failed task can be retried: FAILED -> READY -> a second attempt reuses the worktree.
    env.tasks.transition(a.id, "READY", actor="test")
    r2 = env.sup.wait(env.sup.start(a.id).id)
    assert r2.attempt == 2 and r2.status == "SUCCEEDED"


def test_hang_is_killed_by_watchdog(env):
    t = env.new_task()
    started = time.monotonic()
    run = env.sup.wait(env.sup.start(t.id, scenario="hang", hang_timeout_s=1.0).id)
    assert run.status == "HUNG" and time.monotonic() - started < 15
    assert env.tasks.get(t.id).status == "FAILED"
    assert not _alive(run.pid)


def test_max_runtime_timeout(env):
    t = env.new_task()
    run = env.sup.wait(
        env.sup.start(t.id, scenario="slow", scenario_args={"steps": 500}, max_runtime_s=1.0).id
    )
    assert run.status == "TIMEOUT" and env.tasks.get(t.id).status == "FAILED"


def test_budget_burst_stops_at_cap_and_dedups(env):
    t = env.new_task()
    run = env.sup.wait(
        env.sup.start(
            t.id,
            scenario="budget_burst",
            scenario_args={"step_microusd": 400_000, "steps": 50},
            max_budget_microusd=1_000_000,
        ).id  # fmt: skip
    )
    assert run.status == "BUDGET_STOPPED"
    assert env.tasks.get(t.id).status == "PAUSED_BUDGET"
    # every message id is emitted twice but counted once: 400k, 800k, 1.2M >= cap -> 3 rows
    assert run.cost_microusd == 1_200_000
    assert any(e["type"] == "budget_stop" for e in _events(env, run.id))


def test_cancel_kills_the_whole_process_tree(env):
    """Scenario 19: cancelling kills the worker and the grandchild it spawned."""
    t = env.new_task()
    run = env.sup.start(t.id, scenario="grandchild")
    grand = None
    for _ in range(100):
        grand = next((e["pid"] for e in _events(env, run.id) if e["type"] == "grandchild"), None)
        if grand:
            break
        time.sleep(0.1)
    assert grand and _alive(grand) and _alive(run.pid)
    env.sup.stop(run.id, "cancel")
    done = env.sup.wait(run.id)
    assert done.status == "CANCELLED" and env.tasks.get(t.id).status == "CANCELLED"
    time.sleep(0.3)
    for pid in (run.pid, grand):
        try:
            if os.name != "nt":
                os.waitpid(pid, os.WNOHANG)  # reap if it is our child; grandchild is re-parented
        except ChildProcessError:
            pass
        assert not _alive(pid) or _zombie(pid)


def _zombie(pid: int) -> bool:
    if os.name == "nt":
        return False  # no zombies on Windows: a dead process is simply gone
    try:
        return open(f"/proc/{pid}/stat").read().split(")")[-1].split()[0] == "Z"
    except OSError:
        return True


def test_pause_then_resume_same_session_and_worktree(env):
    t = env.new_task()
    run = env.sup.start(t.id, scenario="slow", scenario_args={"steps": 200, "delay_s": 0.05})
    time.sleep(0.5)
    env.sup.stop(run.id, "pause")
    paused = env.sup.wait(run.id)
    assert paused.status == "STOPPED" and paused.session_id
    assert env.tasks.get(t.id).status == "PAUSED"
    r2 = env.sup.start(t.id, scenario="success")
    assert r2.attempt == 2 and r2.worktree_path == run.worktree_path
    assert env.sup.wait(r2.id).status == "SUCCEEDED"
    ev = _events(env, r2.id)
    assert next(e for e in ev if e["type"] == "run_created")["resumed"] is True
    assert next(e for e in ev if e["type"] == "session")["session_id"] == paused.session_id


def test_one_live_run_per_task_and_illegal_states(env):
    t = env.new_task()
    run = env.sup.start(t.id, scenario="slow", scenario_args={"steps": 100})
    with pytest.raises(RunError):
        env.sup.start(t.id)
    env.sup.stop(run.id, "cancel")
    env.sup.wait(run.id)
    with pytest.raises(RunError):  # CANCELLED is terminal
        env.sup.start(t.id)
    backlog = env.tasks.create(env.pid, "not ready", billing_mode="mock")
    with pytest.raises(RunError):
        env.sup.start(backlog.id)


def test_parallel_limit_from_wizard_config(env):
    from cpm.db.settings_repo import set_setting

    set_setting(env.engine, "wizard.config", {"max_parallel_agents": 1})
    a, b = env.new_task("a"), env.new_task("b")
    ra = env.sup.start(a.id, scenario="slow", scenario_args={"steps": 100})
    with pytest.raises(RunError, match="maximum number"):
        env.sup.start(b.id)
    env.sup.stop(ra.id, "cancel")
    env.sup.wait(ra.id)


def test_unknown_scenario_and_cli_mode_refused(env):
    t = env.new_task()
    with pytest.raises(RunRefused):
        env.sup.start(t.id, scenario="rm -rf")
    cli = env.new_task("cli task", billing_mode="cli")
    with pytest.raises(RunRefused, match="stub"):
        env.sup.start(cli.id)
    assert env.tasks.get(cli.id).status == "READY"  # a refused launch changes nothing


def test_shutdown_kills_live_children(env):
    t = env.new_task()
    run = env.sup.start(t.id, scenario="slow", scenario_args={"steps": 1000})
    env.sup.shutdown()
    assert env.sup.get_run(run.id).status == "KILLED"
    assert env.tasks.get(t.id).status == "FAILED"
    assert not _alive(run.pid)


def test_child_environment_has_no_credentials(env, monkeypatch):
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GH_TOKEN",
    ):
        monkeypatch.setenv(name, "leak-me-please-0123456789")
    seen = {}
    from cpm.workers import proctree

    real = proctree.spawn

    def spy(argv, e, cwd):
        seen.update(e)
        return real(argv, e, cwd)

    monkeypatch.setattr(proctree, "spawn", spy)
    t = env.new_task()
    env.sup.wait(env.sup.start(t.id).id)
    assert not [k for k in seen if "ANTHROPIC" in k or "TOKEN" in k or k.startswith("GH_")]
    # untrusted workspace: repo-defined git programs are neutralised
    assert seen["GIT_CONFIG_KEY_0"] == "core.fsmonitor" and seen["GIT_CONFIG_VALUE_0"] == "false"


def test_live_run_gates(env):
    t = env.new_task("live", billing_mode="api")
    with pytest.raises(LiveRunNotAllowed, match="CPM_ALLOW_LIVE_RUNS"):
        env.sup.start(t.id, max_budget_microusd=1_000_000)
    import dataclasses

    env.sup.settings = dataclasses.replace(env.settings, allow_live_runs=True)
    with pytest.raises(LiveRunNotAllowed, match="budget cap"):
        env.sup.start(t.id, max_budget_microusd=1_000_000)
    from cpm.db.settings_repo import set_setting

    set_setting(env.engine, "wizard.config", {"global_budget_microusd": 5_000_000})
    with pytest.raises(LiveRunNotAllowed, match="API key"):
        env.sup.start(t.id, max_budget_microusd=1_000_000)
    env.store.set("anthropic_api_key", "sk-ant-api03-" + "a" * 40)
    with pytest.raises(RunRefused, match="explicit per-run budget"):
        env.sup.start(t.id)
    with pytest.raises(RunRefused, match="ceiling"):
        env.sup.start(t.id, max_budget_microusd=2_000_001)
    with pytest.raises(RunRefused, match="mock tasks"):
        env.sup.start(t.id, scenario="fail", max_budget_microusd=1_000_000)
    assert env.tasks.get(t.id).status == "READY"  # nothing moved, nothing spent


def test_event_log_has_no_secret_and_is_redacted(env):
    t = env.new_task()
    run = env.sup.start(t.id)
    env.sup.wait(run.id)
    env.sup.run_log(run.id).append("text", text="key sk-ant-api03-" + "z" * 30)
    raw = (env.settings.runs_dir / str(run.id) / "events.jsonl").read_text()
    assert "sk-ant-api03" not in raw and "[REDACTED]" in raw
    assert json.loads(raw.splitlines()[0])["seq"] == 1


def test_sigterm_style_child_exit_without_result_is_crash(env, monkeypatch):
    # exit code 0 but no result event must NOT count as success
    from cpm.workers import supervisor

    t = env.new_task()
    run = env.sup.start(t.id, scenario="slow", scenario_args={"steps": 200, "delay_s": 0.05})
    time.sleep(0.4)
    os.kill(run.pid, getattr(signal, "SIGKILL", signal.SIGTERM))  # Windows: TerminateProcess
    done = env.sup.wait(run.id)
    assert done.status == "CRASHED" and supervisor.RUN_ENDED


def test_sdk_budget_error_result_is_a_budget_stop(env):
    t = env.new_task()
    run = env.sup.wait(env.sup.start(t.id, scenario="sdk_budget_error").id)
    assert run.status == "BUDGET_STOPPED"
    assert env.tasks.get(t.id).status == "PAUSED_BUDGET"
    assert run.cost_microusd == 1500  # the crossing response is reconciled from the total
