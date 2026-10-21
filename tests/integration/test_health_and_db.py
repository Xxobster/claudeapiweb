import sqlite3

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cpm.db.engine import make_engine
from cpm.db.migrate import downgrade, upgrade
from cpm.db.models import Project, Task

TABLES = {
    "projects", "tasks", "task_deps", "runs", "events", "reservations",
    "usage_events", "budgets", "project_sources", "settings", "prompts",
}  # fmt: skip


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_revision"] == "0001"


def test_migrations_up_down(tmp_path):
    db = tmp_path / "m.db"
    url = f"sqlite:///{db.as_posix()}"
    upgrade(url)
    assert TABLES <= set(inspect(make_engine(db)).get_table_names())
    downgrade(url)
    assert not (TABLES & set(inspect(make_engine(db)).get_table_names()))
    upgrade(url)  # up again after down


def test_pragmas(tmp_path):
    engine = make_engine(tmp_path / "p.db")
    with engine.connect() as c:
        raw = c.connection.dbapi_connection
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert raw.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_foreign_keys_enforced(tmp_path):
    db = tmp_path / "f.db"
    upgrade(f"sqlite:///{db.as_posix()}")
    with Session(make_engine(db)) as s:
        s.add(Task(project_id=999, title="orphan"))
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            s.commit()


def test_models_roundtrip(tmp_path):
    db = tmp_path / "r.db"
    upgrade(f"sqlite:///{db.as_posix()}")
    with Session(make_engine(db)) as s:
        p = Project(slug="demo", name="Demo", path="C:/x y/\u00e9")
        s.add(p)
        s.flush()
        t = Task(project_id=p.id, title="t", estimate_microusd=1_500_000)
        s.add(t)
        s.commit()
        assert s.get(Task, t.id).status == "DRAFT"
        assert s.get(Task, t.id).estimate_microusd == 1_500_000


def test_wal_file_created(tmp_path):
    db = tmp_path / "w.db"
    upgrade(f"sqlite:///{db.as_posix()}")
    assert sqlite3.connect(db).execute("PRAGMA journal_mode").fetchone()[0] == "wal"
