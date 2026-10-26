import time

from cpm.db.settings_repo import set_setting
from cpm.locks.claims import DesktopClaims
from tests.scheduler.conftest import heavy


def wiz(env, n):
    set_setting(env.engine, "wizard.config", {"max_parallel_agents": n})


def test_scenario_5_two_independent_tasks_run_in_parallel(senv):
    a = senv.task("a", scope=["src/a/"], slow=8)
    b = senv.task("b", scope=["src/b/"], slow=8)
    report = senv.sched.tick()
    assert report.started == [a.id, b.id]
    assert senv.status(a.id) == senv.status(b.id) == "RUNNING"
    assert senv.sup.live_run_for_task(a.id) and senv.sup.live_run_for_task(b.id)
    senv.finish_all()
    senv.sched.tick()
    assert senv.status(a.id) == senv.status(b.id) == "TESTING"  # never COMPLETED (gate = WP10)
    assert senv.held() == []  # reservations released once the runs ended


def test_overlapping_scope_waits_with_visible_reason_then_starts(senv):
    a = senv.task("a", scope=["src/"], slow=6)
    b = senv.task("b", scope=["src/deep/file.py"])
    rep = senv.sched.tick()
    assert rep.started == [a.id]
    assert f"task #{a.id}" in rep.skipped[b.id] and rep.skipped[b.id].startswith("blocked by")
    assert senv.status(b.id) == "READY"
    assert len(senv.events(b.id, "reservation.")) == 1
    senv.sched.tick()
    assert len(senv.events(b.id, "reservation.")) == 1  # one reason, no flood
    senv.finish(a.id)
    assert senv.sched.tick().started == [b.id]  # housekeeping frees a's paths, then b goes
    senv.finish_all()


def test_order_is_deterministic_priority_then_id(senv):
    wiz(senv, 1)
    low = senv.task("low", scope=["a/"], priority=200, slow=2)
    first = senv.task("first", scope=["b/"], priority=10, slow=2)
    twin = senv.task("twin", scope=["c/"], priority=10, slow=2)
    assert senv.sched.tick().started == [first.id]
    senv.finish_all()
    assert senv.sched.tick().started == [twin.id]
    senv.finish_all()
    assert senv.sched.tick().started == [low.id]
    senv.finish_all()


def test_global_limit_comes_from_the_wizard(senv):
    wiz(senv, 2)
    ts = [senv.task(f"t{i}", scope=[f"d{i}/"], slow=6) for i in range(3)]
    rep = senv.sched.tick()
    assert rep.started == [ts[0].id, ts[1].id]
    assert "2 workers already running" in rep.skipped[ts[2].id]
    senv.finish_all()


def test_per_project_limit(senv):
    other = senv.second_project()
    senv.sched.set_config({"per_project_limit": 1})
    a = senv.task("a", scope=["a/"], slow=6)
    b = senv.task("b", scope=["b/"], slow=6)
    c = senv.base.tasks.create(other, "c", billing_mode="mock", declared_scope=["z/"])
    senv.tasks.mark_ready(c.id, actor="t")
    senv.args[c.id] = {"scenario": "slow", "scenario_args": {"steps": 6, "delay_s": 0.1}}
    rep = senv.sched.tick()
    assert rep.started == [a.id, c.id]
    assert "project already runs 1" in rep.skipped[b.id]
    senv.finish_all()


def test_only_one_heavy_task_and_shorter_goes_first(senv):
    long = senv.task("long", scope=["a/"], slow=6, extra=[heavy()], estimate_microusd=900)
    short = senv.task("short", scope=["b/"], slow=2, extra=[heavy()], estimate_microusd=100)
    light = senv.task("light", scope=["c/"], slow=2)
    rep = senv.sched.tick()
    assert rep.started == [light.id, short.id]  # long heavy yields to the shorter heavy one
    assert "heavy task is running" in rep.skipped[long.id]
    senv.finish_all()
    assert senv.sched.tick().started == [long.id]
    senv.finish_all()


def test_desktop_claim_blocks_and_is_reported(senv):
    DesktopClaims(senv.engine).add(senv.pid, "Assets/Game/UI/")
    t = senv.task("ui", scope=["Assets/Game/UI/menu.prefab"])
    rep = senv.sched.tick()
    assert not rep.started and "Desktop claim" in rep.skipped[t.id]
    assert senv.status(t.id) == "READY" and senv.held() == []


def test_refused_start_unwinds_defers_once_and_backs_off(senv):
    t = senv.task("api", scope=["x/"], billing_mode="api")  # live runs are off
    rep = senv.sched.tick()
    assert not rep.started and rep.skipped[t.id].startswith("deferred: live runs are off")
    assert senv.status(t.id) == "READY" and senv.held() == []
    assert len(senv.events(t.id)) == 1
    rep2 = senv.sched.tick()
    assert "backing off" in rep2.skipped[t.id]
    assert len(senv.events(t.id)) == 1


def test_no_secret_spend_path_for_api_tasks(senv):
    """No key, no live flag: nothing is ever launched for an API task (CLAUDE.md rule 7)."""
    t = senv.task("api", billing_mode="api")
    for _ in range(3):
        senv.sched.tick()
    assert senv.sup.list_runs(t.id) == []


def test_pause_stops_dispatch_but_not_housekeeping(senv):
    a = senv.task("a", scope=["a/"], slow=3)
    senv.sched.set_paused(True)
    assert senv.sched.tick().started == []
    assert senv.status(a.id) == "READY"
    senv.sched.set_paused(False)
    assert senv.sched.tick().started == [a.id]
    senv.sched.set_paused(True)
    senv.finish(a.id)
    senv.sched.tick()
    assert senv.held() == []  # released while paused
    assert senv.sched.status()["paused"] is True


def test_stale_reserved_task_is_put_back(senv):
    a = senv.task("a", scope=["a/"])
    senv.adm.reserve(a.id)  # reserved, then "CPM crashed" before start
    assert senv.status(a.id) == "RESERVED"
    rep = senv.sched.tick()
    assert a.id in rep.demoted or a.id in rep.started  # demoted, then re-dispatched at once
    senv.finish_all()


def test_failed_task_is_retried_within_bounds(senv):
    senv.sched.set_config({"retry_delay_s": 0, "max_auto_retries": 2})
    t = senv.task("flaky", scope=["f/"])
    senv.args[t.id] = {"scenario": "fail", "heartbeat_s": 0.2}
    senv.sched.tick()
    senv.finish(t.id)
    senv.sched.tick()  # housekeeping + retry (FAILED -> READY, then dispatched again)
    assert senv.tasks.get(t.id).retry_count == 1
    assert [e.kind for e in senv.events(t.id)] == ["scheduler.retry"]
    senv.finish(t.id)


def test_repeated_identical_failure_gives_up_once(senv):
    senv.sched.set_config({"retry_delay_s": 0, "max_auto_retries": 5, "identical_failure_limit": 2})
    t = senv.task("flaky", scope=["f/"])
    senv.args[t.id] = {"scenario": "fail", "heartbeat_s": 0.2}
    for _ in range(6):
        senv.sched.tick()
        senv.finish(t.id)
    senv.sched.tick()
    assert senv.status(t.id) == "FAILED"
    assert len(senv.sup.list_runs(t.id)) == 2  # not a third attempt
    gave = [e for e in senv.events(t.id) if e.kind == "scheduler.gave_up"]
    assert len(gave) == 1 and "same failure 2 times" in gave[0].detail


def test_dependents_are_promoted_when_dependencies_are_done(senv):
    a = senv.task("a", scope=["a/"])
    b = senv.base.tasks.create(senv.pid, "b", billing_mode="mock", declared_scope=["b/"])
    senv.tasks.add_dependency(b.id, a.id, actor="t")
    senv.tasks.mark_ready(b.id, actor="t")
    assert senv.status(b.id) == "BLOCKED"
    senv.tasks.transition(a.id, "RESERVED", actor="t")
    senv.tasks.transition(a.id, "RUNNING", actor="t")
    senv.tasks.transition(a.id, "TESTING", actor="t")
    senv.tasks.transition(a.id, "REVIEWING", actor="t")
    senv.tasks.transition(a.id, "COMPLETED", actor="gate", evidence={"acceptance_gate": "passed"})
    rep = senv.sched.tick()
    assert b.id in rep.promoted and b.id in rep.started
    senv.finish_all()


def test_hand_blocked_task_is_not_promoted(senv):
    a = senv.task("a")
    b = senv.base.tasks.create(senv.pid, "b", billing_mode="mock")
    senv.tasks.add_dependency(b.id, a.id, actor="t")
    senv.tasks.transition(b.id, "BLOCKED", actor="user", reason="waiting for Micah's art")
    senv.tasks.transition(b.id, "BACKLOG", actor="t")
    senv.tasks.transition(b.id, "BLOCKED", actor="user", reason="my own reason")
    assert b.id not in senv.sched.tick().promoted


def test_startup_sweeps_and_tick_serialises(senv):
    assert senv.sched.tick().errors == []
    senv.sched._tick_lock.acquire()
    try:
        assert senv.sched.tick().errors == ["another tick is running"]
    finally:
        senv.sched._tick_lock.release()


def test_loop_runs_ticks(senv):
    import asyncio

    from cpm.scheduler.core import run_forever

    a = senv.task("a", scope=["a/"], slow=2)

    async def go():
        t = asyncio.create_task(run_forever(senv.sched, 0.05))
        for _ in range(100):
            if senv.sup.list_runs(a.id):
                break
            await asyncio.sleep(0.05)
        t.cancel()

    asyncio.run(go())
    assert senv.sup.list_runs(a.id)
    senv.finish_all()
    time.sleep(0)


def test_failed_launch_keeps_failed_state_so_retry_bounds_apply(senv):
    """A start that fails after the task left RESERVED must not be undone to READY."""
    senv.sched.set_config({"max_auto_retries": 0})
    t = senv.task("x", scope=["x/"])
    real = senv.sup.start

    def boom(task_id, **kw):
        senv.tasks.transition(task_id, "RUNNING", actor="t")
        senv.tasks.transition(task_id, "FAILED", actor="t")
        raise RuntimeError("spawn failed")

    senv.sup.start = boom
    try:
        senv.sched.tick()
    finally:
        senv.sup.start = real
    assert senv.status(t.id) == "FAILED" and senv.held() == []
    senv.sched.tick()
    assert senv.status(t.id) == "FAILED"  # not relaunched: retries are off


def test_unwind_does_not_undo_a_user_pause(senv):
    t = senv.task("x", scope=["x/"])
    real = senv.sup.start

    def pause_then_fail(task_id, **kw):
        senv.tasks.transition(task_id, "PAUSED", actor="user")
        raise RuntimeError("spawn failed")

    senv.sup.start = pause_then_fail
    try:
        senv.sched.tick()
    finally:
        senv.sup.start = real
    assert senv.status(t.id) == "PAUSED" and senv.held() == []


def test_stop_drains_and_blocks_new_ticks(senv):
    senv.task("x", scope=["x/"], slow=2)
    senv.sched.stop()
    rep = senv.sched.tick()
    assert rep.errors == ["scheduler is stopping"] and rep.started == []


def test_queue_view_orders_and_explains_waiting_tasks(senv):
    DesktopClaims(senv.engine).add(senv.pid, "Assets/Game/UI/")
    late = senv.task("late", scope=["z/"], priority=200, slow=2)
    ui = senv.task("ui", scope=["Assets/Game/UI/menu.prefab"], priority=50)
    api_t = senv.task("api", scope=["x/"], priority=100, billing_mode="api")
    before = senv.sched.queue()
    assert [r["task_id"] for r in before["ready"]] == [ui.id, api_t.id, late.id]
    assert [r["position"] for r in before["ready"]] == [1, 2, 3]
    assert all(r["reason"] is None for r in before["ready"]) and before["running"] == []
    senv.sched.tick()
    q = senv.sched.queue()
    by = {r["task_id"]: r for r in q["ready"]}
    assert "Desktop claim" in by[ui.id]["reason"]
    assert by[api_t.id]["reason"].startswith("deferred:") and by[api_t.id]["backoff_s"] > 0
    assert by[api_t.id]["refused_starts"] == 1
    assert [r["task_id"] for r in q["running"]] == [late.id]
    senv.finish_all()
    # after the next tick the reason survives from the stored events, not just the last pass
    senv.sched._last = None
    again = {r["task_id"]: r for r in senv.sched.queue()["ready"]}
    assert again[ui.id]["reason_source"] == "event" and "Desktop claim" in again[ui.id]["reason"]


def test_queue_view_previews_automatic_retry(senv):
    senv.sched.set_config({"retry_delay_s": 0, "max_auto_retries": 0})
    t = senv.task("flaky", scope=["f/"])
    senv.args[t.id] = {"scenario": "fail", "heartbeat_s": 0.2}
    senv.sched.tick()
    senv.finish(t.id)
    row = senv.sched.queue()["failed"][0]
    assert row["task_id"] == t.id and row["action"] == "give_up" and "limit" in row["why"]
