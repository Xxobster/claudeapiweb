import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cpm.db.models import BudgetReservation, Reservation, Run, utcnow
from cpm.locks.reservations import ReservationService
from cpm.locks.types import ResourceRequest
from cpm.recovery.reconcile import Recovery
from tests.workers.conftest import env as worker_env  # noqa: F401  (fixture reused)


class RecEnv:
    def __init__(self, base):
        self.base = base
        self.engine, self.tasks, self.settings = base.engine, base.tasks, base.settings
        self.res = ReservationService(self.engine)
        self.live: set[int] = set()
        self.rec = Recovery(
            self.engine, self.settings, is_live=lambda r: r in self.live, reservations=self.res
        )

    def orphan(self, title="orphan", *, task_status="RUNNING", run=True, log_events=(), pid=None):
        """A task and run as a crashed CPM would leave them: no process, nothing finalised."""
        t = self.base.new_task(title, declared_scope=[f"src/{title}/"])
        self.tasks.transition(t.id, "RESERVED", actor="test")
        if task_status == "RUNNING":
            self.tasks.transition(t.id, "RUNNING", actor="test")
        self.res.acquire(
            t.id, self.base.pid, [ResourceRequest("path", f"src/{title}/", "excl", "scope")]
        )
        run_id = None
        if run:
            with Session(self.engine) as s:
                r = Run(
                    task_id=t.id,
                    attempt=1,
                    billing_mode="mock",
                    status="RUNNING",
                    pid=pid,
                    started_at=utcnow(),
                    heartbeat_at=utcnow(),
                )
                s.add(r)
                s.flush()
                run_id = r.id
                s.add(BudgetReservation(
                    project_id=self.base.pid, task_id=t.id, run_id=run_id,
                    amount_microusd=1000, status="held",
                ))  # fmt: skip
                s.commit()
            d = self.settings.runs_dir / str(run_id)
            d.mkdir(parents=True)
            (d / "events.jsonl").write_text(
                "".join(json.dumps({"seq": i + 1, **e}) + "\n" for i, e in enumerate(log_events)),
                encoding="utf-8",
            )
        return t.id, run_id

    def reservations(self):
        with Session(self.engine) as s:
            return sorted({r.task_id for r in s.scalars(select(Reservation))})

    def status(self, task_id):
        return self.tasks.get(task_id).status


@pytest.fixture
def renv(worker_env):  # noqa: F811
    return RecEnv(worker_env)
