import pytest
from sqlalchemy.orm import Session

from cpm.config import Settings
from cpm.db.engine import make_engine
from cpm.db.migrate import upgrade
from cpm.db.models import Project
from cpm.db.settings_repo import set_setting
from cpm.security.secrets_store import MemorySecretStore
from cpm.tasks.service import TaskService
from cpm.workers.supervisor import RunSupervisor
from tests.fixtures.repos import make_repo


@pytest.fixture
def env(tmp_path):
    """Migrated DB, one fixture git repo registered as a project, supervisor with mock runs."""
    settings = Settings(
        host="127.0.0.1", port=8765, data_dir=tmp_path / "data", log_level="INFO",
        worktree_root=tmp_path / "worktrees",
    )  # fmt: skip
    settings.data_dir.mkdir(parents=True)
    upgrade(settings.db_url)
    engine = make_engine(settings.db_path)
    repo = make_repo(tmp_path / "managed project")
    with Session(engine) as s:
        p = Project(slug="demo", name="Demo", path=str(repo))
        s.add(p)
        s.commit()
        pid = p.id
    set_setting(engine, "approved_roots", [str(repo)])
    store = MemorySecretStore()
    sup = RunSupervisor(engine, settings, store)
    tasks = TaskService(engine)

    class Env:
        pass

    e = Env()
    e.settings, e.engine, e.repo, e.pid = settings, engine, repo, pid  # type: ignore[attr-defined]
    e.store, e.sup, e.tasks = store, sup, tasks  # type: ignore[attr-defined]

    def new_task(title="Do the thing", **kw):
        kw.setdefault("billing_mode", "mock")
        t = tasks.create(pid, title, **kw)
        return tasks.mark_ready(t.id, actor="test")

    e.new_task = new_task  # type: ignore[attr-defined]
    yield e
    sup.shutdown()
    engine.dispose()
