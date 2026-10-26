"""Regression tests for the WP5 safety audit (process tree, environment, paths, budget)."""

import dataclasses
import io
import os
import sys

import pytest

from cpm.db.models import Run, UsageEvent
from cpm.db.settings_repo import set_setting
from cpm.security.childenv import build_mock_env
from cpm.workers import proctree
from cpm.workers.events import RunLog
from cpm.workers.permissions import ToolContext, decide
from cpm.workers.supervisor import MAX_LINE, RunError, RunRefused, _lines


def _live_ready(env):
    env.sup.settings = dataclasses.replace(env.settings, allow_live_runs=True)
    set_setting(env.engine, "wizard.config", {"global_budget_microusd": 9_000_000})
    env.store.set("anthropic_api_key", "sk-ant-api03-" + "a" * 40)


def test_task_budget_is_shared_by_all_runs_of_a_task(env):
    from sqlalchemy.orm import Session

    _live_ready(env)
    t = env.new_task("live", billing_mode="api")
    with Session(env.engine) as s:
        old = Run(task_id=t.id, attempt=1, billing_mode="api", status="FAILED")
        s.add(old)
        s.flush()
        s.add(
            UsageEvent(run_id=old.id, task_id=t.id, project_id=env.pid, dedup_key="msg:old",
                       model="m", cost_microusd=1_500_000)
        )  # fmt: skip
        s.commit()
    with pytest.raises(RunRefused, match="task budget left is 500000"):
        env.sup.start(t.id, max_budget_microusd=1_000_000)
    assert env.tasks.get(t.id).status == "READY"


def test_project_path_must_still_be_in_an_approved_root(env):
    set_setting(env.engine, "approved_roots", [])
    t = env.new_task()
    with pytest.raises(RunRefused, match="approved root"):
        env.sup.start(t.id)
    assert env.tasks.get(t.id).status == "READY"


def test_cancel_before_launch_prevents_the_spawn(env, monkeypatch):
    t = env.new_task()
    monkeypatch.setattr(env.sup, "_task_status", lambda _id: "CANCELLED")
    with pytest.raises(RunError, match="stopped before"):
        env.sup.start(t.id)
    assert env.sup.live_run_for_task(t.id) is None


def test_cancel_racing_the_spawn_kills_the_worker(env, monkeypatch):
    t = env.new_task()
    answers = iter(["RUNNING", "CANCELLED"])
    monkeypatch.setattr(env.sup, "_task_status", lambda _id: next(answers))
    run = env.sup.start(t.id, scenario="slow", scenario_args={"steps": 1000})
    assert env.sup.wait(run.id).status == "CANCELLED"


def test_oversized_output_line_is_dropped_not_buffered():
    big = b"x" * (MAX_LINE * 3) + b"\n"
    got = list(_lines(io.BufferedReader(io.BytesIO(b'{"type":"a"}\n' + big + b'{"type":"b"}\n'))))
    assert got == [b'{"type":"a"}\n', None, b'{"type":"b"}\n']


def test_kill_after_close_is_a_noop(tmp_path):
    tree = proctree.spawn(
        [sys.executable, "-c", "import time; time.sleep(60)"], os.environ, tmp_path
    )
    tree.kill()
    tree.popen.wait(10)
    tree.close()
    tree.kill()  # e.g. a pause timer firing late: must not signal a recycled pid/group
    assert tree.popen.returncode is not None


def test_child_path_drops_relative_entries():
    sep = os.pathsep
    env = build_mock_env(parent={"PATH": f".{sep}{sep}rel{sep}{os.path.abspath(os.sep)}"})
    assert env["PATH"] == os.path.abspath(os.sep)
    assert env["NoDefaultCurrentDirectoryInExePath"] == "1"


@pytest.mark.parametrize(
    "path", [".Claude/settings.json", ".GIT/hooks/pre-commit", "sub/.Mcp.json", "CLAUDE~1/x.json",
             ".git./config", ".GITMODULES"]
)  # fmt: skip
def test_protected_paths_are_case_and_shortname_insensitive(tmp_path, path):
    d = decide("Write", {"file_path": path}, ToolContext(tmp_path))
    assert not d.allow


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
def test_ls_and_pytest_follow_no_symlink_out_of_the_worktree(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "link").symlink_to(outside)
    ctx = ToolContext(wt, trusted=True)
    assert not decide("Bash", {"command": "ls link"}, ctx).allow
    assert not decide("Bash", {"command": "python -m pytest link"}, ctx).allow
    assert decide("Bash", {"command": "ls ."}, ctx).allow
    assert decide("Bash", {"command": "python -m pytest tests/test_a.py::test_x -q"}, ctx).allow


def test_log_replay_skips_without_changing_results(tmp_path):
    log = RunLog(tmp_path / "e.jsonl")
    for i in range(5):
        log.append("text", n=i)
    assert [e["seq"] for e in log.read(3)] == [4, 5]
    assert len(log.read(0)) == 5


def test_pause_racing_the_spawn_stays_paused_and_resumable(env, monkeypatch):
    t = env.new_task()
    answers = iter(["RUNNING", "PAUSED"])
    monkeypatch.setattr(env.sup, "_task_status", lambda _id: next(answers))
    run = env.sup.start(t.id, scenario="slow", scenario_args={"steps": 1000})
    assert env.sup.wait(run.id).status == "STOPPED"
