# ruff: noqa: E501
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
    "usage_events", "budgets", "prices", "budget_reservations", "project_sources", "settings", "prompts",
}  # fmt: skip


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_revision"] == "0006"


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
        assert s.get(Task, t.id).status == "BACKLOG"
        assert s.get(Task, t.id).estimate_microusd == 1_500_000


def test_wal_file_created(tmp_path):
    db = tmp_path / "w.db"
    upgrade(f"sqlite:///{db.as_posix()}")
    assert sqlite3.connect(db).execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_upgrade_0002_keeps_referencing_rows(tmp_path):
    """Table rebuild during migration must not cascade-delete deps, runs, usage or events."""
    db = tmp_path / "pop.db"
    url = f"sqlite:///{db.as_posix()}"
    upgrade(url, "0001")
    engine = make_engine(db)
    with engine.begin() as c:
        raw = c.connection.dbapi_connection
        raw.executescript(
            """
            INSERT INTO projects (id, slug, name, path, integration_mode, workspace_trusted, created_at)
                VALUES (1, 'p', 'P', '/x', 'pr_only', 0, '2026-01-01');
            INSERT INTO tasks (id, project_id, title, description, status, priority, billing_mode,
                declared_scope, acceptance, estimate_microusd, created_at, updated_at)
                VALUES (1, 1, 'a', '', 'DRAFT', 100, 'api', '[]', '[]', 0, '2026-01-01', '2026-01-01'),
                       (2, 1, 'b', '', 'READY', 100, 'api', '[]', '[]', 0, '2026-01-01', '2026-01-01');
            INSERT INTO task_deps (task_id, depends_on_id) VALUES (2, 1);
            INSERT INTO runs (id, task_id, attempt, billing_mode, status) VALUES (1, 1, 1, 'api', 'DONE');
            INSERT INTO events (id, ts, project_id, task_id, run_id, kind, detail)
                VALUES (1, '2026-01-01', 1, 1, 1, 'x', '{}');
            INSERT INTO usage_events (id, ts, run_id, task_id, project_id, dedup_key, model,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_microusd, reconciled)
                VALUES (1, '2026-01-01', 1, 1, 1, 'k', 'm', 1, 1, 0, 0, 5, 0);
            """
        )
    upgrade(url)
    with engine.connect() as c:
        q = lambda sql: c.exec_driver_sql(sql).scalar()  # noqa: E731
        assert q("SELECT count(*) FROM task_deps") == 1
        assert q("SELECT count(*) FROM runs") == 1
        assert q("SELECT count(*) FROM usage_events") == 1
        assert q("SELECT task_id FROM events") == 1
        assert q("SELECT status FROM tasks WHERE id=1") == "BACKLOG"
        assert q("PRAGMA foreign_keys") == 1


def test_migration_0003_matches_models_and_downgrades(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from cpm.db.migrate import downgrade
    from cpm.db.models import Base

    db = tmp_path / "m.db"
    url = f"sqlite:///{db.as_posix()}"
    upgrade(url)
    engine = make_engine(db)
    with engine.connect() as c:
        diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
    names = {d[0] for d in diff}
    assert not names & {"add_table", "remove_table", "add_column", "remove_column"}, diff
    downgrade(url, "0002")
    with engine.connect() as c:
        assert not c.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE name='desktop_claims'"
        ).fetchall()
        cols = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(reservations)")}
    assert "project_id" not in cols and "label" not in cols
    upgrade(url)
