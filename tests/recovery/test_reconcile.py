import json
import os
import subprocess
import sys
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from cpm.db.models import BudgetReservation, Event, Run
from cpm.recovery.reconcile import open_step


def run_row(env, run_id):
    with Session(env.engine) as s:
        return s.get(Run, run_id)


def holds(env):
    with Session(env.engine) as s:
        return [h.status for h in s.scalars(select(BudgetReservation))]


def test_scenario_12_orphaned_running_task_is_recovered_once(renv):
    tid, rid = renv.orphan()
    rep = renv.rec.reconcile()
    assert [(o.run_id, o.task_id, o.outcome) for o in rep.runs] == [(rid, tid, "failed")]
    assert renv.status(tid) == "FAILED"
    r = run_row(renv, rid)
    assert r.status == "CRASHED" and r.ended_at is not None
    assert renv.reservations() == [] and holds(renv) == ["released"]
    again = renv.rec.reconcile()  # idempotent: nothing left to do
    assert not again.changed
    with Session(renv.engine) as s:
        kinds = [e.kind for e in s.scalars(select(Event).where(Event.task_id == tid))]
    assert kinds.count("recovery.run_orphaned") == 1


def test_open_non_idempotent_step_pauses_instead_of_failing(renv):
    tid, rid = renv.orphan(log_events=[
        {"type": "permission", "tool": "Bash", "allowed": True},
        {"type": "step_begin", "step": "git commit", "idempotent": False},
    ])  # fmt: skip
    rep = renv.rec.reconcile()
    assert rep.runs[0].outcome == "paused" and rep.runs[0].open_step == "git commit"
    assert renv.status(tid) == "PAUSED"


def test_finished_step_does_not_pause(renv, tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_text(
        "\n".join(json.dumps(e) for e in (
            {"type": "step_begin", "step": "git add"}, {"type": "step_end", "step": "git add"},
            {"type": "text"},
        )) + "\n{partial", encoding="utf-8",
    )  # fmt: skip
    assert open_step(p) is None
    assert open_step(tmp_path / "missing.jsonl") is None


def test_live_run_is_left_alone(renv):
    tid, rid = renv.orphan()
    renv.live.add(rid)
    rep = renv.rec.reconcile()
    assert not rep.runs and renv.status(tid) == "RUNNING" and renv.reservations() == [tid]
    assert holds(renv) == ["held"]


def test_reserved_task_without_run_goes_back_to_ready(renv):
    tid, _ = renv.orphan("r", task_status="RESERVED", run=False)
    rep = renv.rec.reconcile()
    assert rep.tasks_demoted == [tid] and renv.status(tid) == "READY"
    assert renv.reservations() == []


def test_running_task_without_run_fails(renv):
    tid, _ = renv.orphan("n", run=False)
    renv.rec.reconcile()
    assert renv.status(tid) == "FAILED" and renv.reservations() == []


def test_reservation_of_a_finished_task_is_released(renv):
    tid, _ = renv.orphan("done", run=False)
    renv.tasks.transition(tid, "FAILED", actor="test")  # left its reservation behind
    assert renv.reservations() == [tid]
    rep = renv.rec.reconcile()
    assert rep.reservations_released >= 1 and renv.reservations() == []


def test_recovery_never_starts_or_completes_anything(renv):
    ready = renv.base.new_task("waiting")
    tid, _ = renv.orphan()
    renv.rec.reconcile()
    assert renv.status(ready.id) == "READY"
    assert renv.status(tid) not in ("RUNNING", "TESTING", "COMPLETED", "INTEGRATED")


def test_leftover_worker_killed_only_when_it_matches_this_run(renv):
    if not sys.platform.startswith("linux"):
        return
    tid, rid = renv.orphan("k")
    spec = str(renv.settings.runs_dir / str(rid) / "spec.json")
    code = "import time; time.sleep(60)"
    worker = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", code, "cpm.workers.run", spec], start_new_session=True
    )
    stranger = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)  # noqa: S603
    try:
        with Session(renv.engine) as s:
            s.get(Run, rid).pid = worker.pid
            s.commit()
        rep = renv.rec.reconcile()
        assert rep.runs[0].process_killed is True
        assert worker.wait(5) is not None
        # same call for a pid that is not this run's worker: never killed
        tid2, rid2 = renv.orphan("k2", pid=stranger.pid)
        rep2 = renv.rec.reconcile()
        assert [o.process_killed for o in rep2.runs if o.run_id == rid2] == [False]
        time.sleep(0.2)
        assert stranger.poll() is None
    finally:
        for p in (worker, stranger):
            p.kill()
            p.wait()


def test_orphan_worktrees_are_reported_never_deleted(renv):
    base = renv.base
    t = base.new_task("wt")
    base.sup.worktrees.ensure(base.repo, "demo", t.id, "wt")
    wt = base.sup.worktrees.path_for("demo", t.id)
    (wt / "unsaved.txt").write_text("precious", encoding="utf-8")
    renv.tasks.transition(t.id, "CANCELLED", actor="test")
    rep = renv.rec.reconcile()
    note = next(n for n in rep.worktrees if n.task_id == t.id)
    assert note.dirty and "kept" in note.note
    assert (wt / "unsaved.txt").read_text(encoding="utf-8") == "precious"
    assert os.path.isdir(wt)
    listed = subprocess.run(  # noqa: S603
        ["git", "-C", str(base.repo), "worktree", "list"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert wt.name in listed


def test_worker_dying_inside_a_non_idempotent_step_pauses_the_task(renv):
    """Live path (CPM stays up): a crash inside `git commit` is not retried automatically."""
    t = renv.base.new_task("half")
    run = renv.base.sup.wait(renv.base.sup.start(t.id, scenario="half_done", heartbeat_s=0.2).id)
    assert run.status == "CRASHED"
    assert renv.status(t.id) == "PAUSED"
    events = renv.base.sup.run_log(run.id).read()
    assert [e["type"] for e in events if e["type"].startswith("step_")] == ["step_begin"]


def test_a_finished_step_is_closed_in_the_log(renv):
    t = renv.base.new_task("full")
    run = renv.base.sup.wait(renv.base.sup.start(t.id, scenario="success").id)
    assert run.status == "SUCCEEDED" and renv.status(t.id) == "TESTING"
