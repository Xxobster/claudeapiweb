"""Lanes wired into tasks, the supervisor, the scheduler and the acceptance gate (mock workers,
no key, no network)."""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cpm.db.models import Run
from cpm.lanes.registry import Finding, Unavailable, register_validator
from cpm.tasks.service import InvalidTask
from cpm.workers.supervisor import RunRefused
from tests.lanes.conftest import LANE, make_profile

OK = {"assets/out/a.txt": "hello"}


# ------------------------------------------------------------------ tasks
def test_lane_must_exist_in_the_profile(lenv):
    with pytest.raises(InvalidTask):
        lenv.tasks.create(lenv.pid, "x", lane="nope")
    t = lenv.tasks.create(lenv.pid, "x", lane="test")
    assert t.lane == "test"
    assert lenv.tasks.edit(t.id, actor="u", lane="").lane is None
    assert lenv.tasks.edit(t.id, actor="u", lane="test").lane == "test"
    with pytest.raises(InvalidTask):
        lenv.tasks.edit(t.id, actor="u", lane="nope")


def test_lane_cannot_change_while_the_task_is_active(lenv):
    from cpm.tasks.service import IllegalTransition

    t = lenv.tasks.create(lenv.pid, "x", lane="test", billing_mode="mock")
    lenv.tasks.mark_ready(t.id, actor="u")
    lenv.tasks.transition(t.id, "RESERVED", actor="scheduler")
    with pytest.raises(IllegalTransition):
        lenv.tasks.edit(t.id, actor="u", lane="")


# ------------------------------------------------------------------ scheduler hooks
def test_resources_and_limits_come_from_the_lane(lenv):
    t = lenv.tasks.create(lenv.pid, "x", lane="test")
    res = lenv.lanes.resources(t)
    assert [(r.type, r.key) for r in res] == [("gpu", "gpu")]
    assert lenv.lanes.start_args(t) == {"max_runtime_s": 120, "hang_timeout_s": 30}
    plain = lenv.tasks.create(lenv.pid, "plain")
    assert lenv.lanes.resources(plain) == [] and lenv.lanes.start_args(plain) == {}


def test_a_broken_lane_reserves_nothing_and_the_start_is_refused(lenv):
    t = lenv.tasks.create(lenv.pid, "x", lane="test", billing_mode="mock")
    lenv.tasks.mark_ready(t.id, actor="u")
    lenv.e_profile = lenv.profile
    lenv.profile = make_profile(dict(LANE, enabled=False))
    assert lenv.lanes.resources(t) == []
    with pytest.raises(RunRefused, match="disabled"):
        lenv.sup.start(t.id)
    lenv.profile = make_profile()  # lane removed from a different profile
    lenv.profile.lanes.clear()
    with pytest.raises(RunRefused, match="not defined"):
        lenv.sup.start(t.id)
    assert lenv.tasks.get(t.id).status == "READY"  # nothing started, nothing charged


def test_missing_tool_defers_the_start_before_any_budget_is_held(lenv, tmp_path):
    tool = tmp_path / "bin" / "renderer"
    lenv.profile = make_profile(dict(LANE, required_tools={"renderer": str(tool)}))
    t = lenv.tasks.create(lenv.pid, "x", lane="test", billing_mode="mock")
    lenv.tasks.mark_ready(t.id, actor="u")
    with pytest.raises(RunRefused, match="renderer"):
        lenv.sup.start(t.id)
    assert lenv.tasks.get(t.id).status == "READY"
    with Session(lenv.engine) as s:
        assert s.scalar(select(Run.id)) is None
    tool.parent.mkdir()
    tool.write_text("x")
    assert lenv.sup.start(t.id, heartbeat_s=0.2)


def test_prompt_carries_the_lane_section_outside_the_task_data(lenv):
    spec = lenv.profile.lanes["test"]
    ctx = lenv.lanes.context(lenv.pid, "test")
    text = lenv.lanes.prompt_section(ctx)
    assert "LANE: test - Test lane" in text
    assert "assets/out/" in text and "renderer" in text and "Summary, Checks" in text
    assert "{asset_paths}" not in text and spec.prompt.startswith("Write into {asset_paths}")


# ------------------------------------------------------------------ gate
def test_good_lane_output_passes_the_gate(lenv):
    tid = lenv.lane_task(OK)
    out = lenv.gate.run(tid)
    assert (out.action, out.task_status) == ("completed", "COMPLETED")
    assert out.summary["lane"]["status"] == "passed"


def test_missing_validator_output_sends_the_task_back_with_feedback(lenv):
    tid = lenv.lane_task({"assets/out/a.md": "x"}, scope=["assets/"])
    out = lenv.gate.run(tid)
    assert (out.outcome, out.action) == ("lane_failed", "correction")
    codes = {f["code"] for f in out.summary["lane"]["findings"]}
    assert {"extension", "too_few_files"} <= codes


def test_output_outside_the_asset_paths_is_a_correction(lenv):
    tid = lenv.lane_task({"assets/out/a.txt": "x", "assets/other/b.txt": "y"}, scope=["assets/"])
    out = lenv.gate.run(tid)
    assert out.outcome == "lane_failed"
    assert [
        f["path"] for f in out.summary["lane"]["findings"] if f["code"] == "outside_assets"
    ] == ["assets/other/b.txt"]


def test_missing_report_section_is_a_correction(lenv):
    tid = lenv.lane_task(OK, report="## Summary\nonly this")
    out = lenv.gate.run(tid)
    assert out.outcome == "lane_failed"
    assert any("Checks" in f["message"] for f in out.summary["lane"]["findings"])


def test_binary_waits_for_a_person_then_lfs_policy_is_honoured(lenv):
    files = {"assets/out/a.txt": "x", "assets/out/line.wav": 1000}
    tid = lenv.lane_task(files)
    out = lenv.gate.run(tid)
    assert (out.outcome, out.action, out.task_status) == ("lane_waiting", "waiting", "TESTING")
    assert out.summary["lane"]["decisions"] == ["assets/out/line.wav"]
    assert "binary" in out.message

    lenv.profile = make_profile(dict(LANE, artifacts=dict(LANE["artifacts"], binary_policy="deny")))
    tid2 = lenv.lane_task(files)
    assert lenv.gate.run(tid2).outcome == "lane_failed"


def test_lane_limit_lets_a_big_asset_through_where_the_default_would_not(lenv):
    big = 6_000_000  # above the gate's 5 MB generic limit
    lenv.profile = make_profile(
        dict(LANE, artifacts=dict(LANE["artifacts"], max_file_bytes=9_000_000, extensions=[".txt"]))
    )
    tid = lenv.lane_task({"assets/out/a.txt": big})
    out = lenv.gate.run(tid)
    assert out.summary.get("unexpected") == []
    assert out.task_status == "COMPLETED"
    # and the generic rule still applies to a task without a lane
    plain = lenv.new_task("plain", acceptance=["exists"])
    run = lenv.sup.start(
        plain.id, scenario="files", scenario_args={"files": {"b.txt": big}}, heartbeat_s=0.2
    )
    lenv.sup.wait(run.id, 30)
    out2 = lenv.gate.run(plain.id)
    assert out2.outcome == "unexpected_files"


def test_unavailable_validator_waits_and_crashing_validator_fails(lenv):
    def need_pc(_ctx, _params):
        raise Unavailable("Blender is not installed on this PC")

    def boom(_ctx, _params):
        raise RuntimeError("bug")

    register_validator("t_need_pc", need_pc)
    register_validator("t_boom", boom)
    lenv.profile = make_profile(dict(LANE, validators=[{"kind": "t_need_pc"}]))
    out = lenv.gate.run(lenv.lane_task(OK))
    assert (out.outcome, out.action) == ("lane_waiting", "waiting")
    assert "Blender" in out.message

    lenv.profile = make_profile(dict(LANE, validators=[{"kind": "t_boom"}]))
    out = lenv.gate.run(lenv.lane_task(OK))
    assert out.outcome == "lane_failed"
    assert out.summary["lane"]["findings"][0]["code"] == "validator_crashed"


def test_unknown_validator_fails_closed_and_warn_severity_does_not_block(lenv):
    lenv.profile = make_profile(dict(LANE, validators=[{"kind": "no_such_thing"}]))
    assert lenv.gate.run(lenv.lane_task(OK)).outcome == "lane_failed"

    register_validator("t_warns", lambda _c, _p: [Finding("meh", "style")])
    lenv.profile = make_profile(dict(LANE, validators=[{"kind": "t_warns", "severity": "warn"}]))
    out = lenv.gate.run(lenv.lane_task(OK))
    assert out.task_status == "COMPLETED"
    assert out.summary["lane"]["findings"][0]["severity"] == "warn"


def test_lane_removed_from_profile_waits_instead_of_passing(lenv):
    tid = lenv.lane_task(OK)
    lenv.profile = make_profile()
    lenv.profile.lanes.clear()
    out = lenv.gate.run(tid)
    assert (out.outcome, out.action, out.task_status) == ("lane_error", "waiting", "TESTING")


def test_plain_tasks_are_untouched_by_lanes(lenv):
    t = lenv.new_task("plain", acceptance=["exists"])
    run = lenv.sup.start(t.id, heartbeat_s=0.2)
    lenv.sup.wait(run.id, 30)
    out = lenv.gate.run(t.id)
    assert "lane" not in out.summary and out.task_status == "COMPLETED"
    spec = json.loads(
        (lenv.settings.runs_dir / str(run.id) / "spec.json").read_text(encoding="utf-8")
    )
    assert spec["lane"] is None


def test_run_spec_carries_commands_not_secrets(lenv):
    tid = lenv.lane_task(OK)
    run = lenv.sup.list_runs(tid)[-1]
    spec = json.loads((lenv.settings.runs_dir / str(run.id) / "spec.json").read_text("utf-8"))
    assert spec["lane"]["lane"] == "test"
    assert [c["program"] for c in spec["lane"]["commands"]] == ["renderer"]
    assert spec["lane"]["assets"] == ["assets/out/"]
