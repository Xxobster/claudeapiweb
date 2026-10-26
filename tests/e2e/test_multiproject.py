"""WP16: realistic multi-project run on the mock provider (no API key, no network)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from cpm.db.models import Reservation, Task


def test_two_projects_run_in_parallel_and_nothing_is_completed_without_the_gate(senv):
    other = senv.second_project()
    a = senv.task("alpha", scope=["src/a/"], slow=6)
    t = senv.tasks.create(other, "beta", declared_scope=["src/a/"], billing_mode="mock")
    b = senv.tasks.mark_ready(t.id, actor="test")  # same relative scope, other project

    report = senv.sched.tick()
    assert set(report.started) == {a.id, b.id}  # scopes are per project: no false conflict
    senv.finish_all()
    senv.sched.tick()

    assert senv.status(a.id) == "TESTING" and senv.status(b.id) == "TESTING"
    with Session(senv.engine) as s:
        assert s.scalars(select(Reservation)).first() is None  # holds released after the runs
        assert {x.project_id for x in s.scalars(select(Task))} == {senv.pid, other}


def test_no_task_is_completed_without_the_gate(senv):
    ts = [senv.task(f"t{i}", scope=[f"d{i}/"], slow=2) for i in range(3)]
    senv.sched.tick()
    senv.finish_all()
    senv.sched.tick()
    with Session(senv.engine) as s:
        statuses = {s.get(Task, t.id).status for t in ts}
        assert statuses and "COMPLETED" not in statuses  # only the gate may complete a task
