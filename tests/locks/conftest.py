import pytest
from sqlalchemy.orm import Session

from cpm.db.engine import make_engine
from cpm.db.migrate import upgrade
from cpm.db.models import Project
from cpm.locks.admission import Admission
from cpm.locks.reservations import ReservationService
from cpm.tasks.service import TaskService
from tests.fixtures.repos import make_repo


class Env:
    def __init__(self, tmp_path):
        db = tmp_path / "t.db"
        upgrade(f"sqlite:///{db.as_posix()}")
        self.engine = make_engine(db)
        self.root = make_repo(tmp_path / "managed")
        self.tmp = tmp_path
        with Session(self.engine) as s:
            p = Project(slug="demo", name="Demo", path=str(self.root))
            s.add(p)
            s.commit()
            self.pid = p.id
        self.live: set[int] = set()
        self.res = ReservationService(self.engine, liveness=lambda t: t in self.live)
        self.adm = Admission(self.engine, self.res)
        self.tasks = TaskService(self.engine)

    def task(self, *scope: str, ready: bool = False, project_id: int | None = None):
        t = self.tasks.create(project_id or self.pid, "t", declared_scope=list(scope))
        if ready:
            t = self.tasks.mark_ready(t.id, actor="test")
        return t

    def other_project(self, name: str = "other") -> int:
        root = make_repo(self.tmp / name)
        with Session(self.engine) as s:
            p = Project(slug=name, name=name, path=str(root))
            s.add(p)
            s.commit()
            return p.id


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)
