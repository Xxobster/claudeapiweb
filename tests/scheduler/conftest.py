import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cpm.db.models import Event, Project, Reservation
from cpm.db.settings_repo import set_setting
from cpm.locks.admission import Admission
from cpm.locks.reservations import ReservationService
from cpm.locks.types import ResourceRequest
from cpm.scheduler.core import Scheduler
from tests.fixtures.repos import make_repo
from tests.workers.conftest import env as worker_env  # noqa: F401  (fixture reused)


class SchedEnv:
    def __init__(self, base):
        self.base = base
        self.engine, self.sup, self.tasks, self.pid = base.engine, base.sup, base.tasks, base.pid
        self.res = ReservationService(
            self.engine, liveness=lambda t: self.sup.live_run_for_task(t) is not None
        )
        self.adm = Admission(self.engine, self.res)
        self.args: dict[int, dict] = {}
        self.extra: dict[int, list] = {}
        self.sched = Scheduler(
            self.engine, self.sup, self.adm, self.res,
            resources_for=lambda t: self.extra.get(t.id, ()),
            start_args_for=lambda t: self.args.get(t.id, {"heartbeat_s": 0.2}),
        )  # fmt: skip

    def task(self, title="t", *, scope=(), priority=100, slow=0, extra=(), **kw):
        t = self.base.new_task(title, declared_scope=list(scope), priority=priority, **kw)
        if slow:
            self.args[t.id] = {
                "scenario": "slow", "scenario_args": {"steps": slow, "delay_s": 0.1},
                "heartbeat_s": 0.2,
            }  # fmt: skip
        if extra:
            self.extra[t.id] = list(extra)
        return t

    def status(self, task_id):
        return self.tasks.get(task_id).status

    def finish(self, task_id):
        for r in self.sup.list_runs(task_id):
            self.sup.wait(r.id, 30)

    def finish_all(self):
        with Session(self.engine) as s:
            from cpm.db.models import Run

            ids = list(s.scalars(select(Run.id)))
        for rid in ids:
            self.sup.wait(rid, 30)

    def events(self, task_id, prefix="scheduler."):
        with Session(self.engine) as s:
            return [
                e for e in s.scalars(select(Event).where(Event.task_id == task_id))
                if e.kind.startswith(prefix)
            ]  # fmt: skip

    def held(self):
        with Session(self.engine) as s:
            return sorted({r.task_id for r in s.scalars(select(Reservation))})

    def second_project(self):
        repo = make_repo(self.base.repo.parent / "second")
        with Session(self.engine) as s:
            p = Project(slug="second", name="Second", path=str(repo))
            s.add(p)
            s.commit()
            pid = p.id
        from cpm.db.settings_repo import get_setting

        set_setting(
            self.engine, "approved_roots", [*get_setting(self.engine, "approved_roots"), str(repo)]
        )
        return pid


def heavy(name="gpu"):
    return ResourceRequest(name, name, "excl", name)


@pytest.fixture
def senv(worker_env):  # noqa: F811
    return SchedEnv(worker_env)
