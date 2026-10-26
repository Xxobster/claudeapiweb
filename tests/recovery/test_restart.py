"""Scenarios 11 and 18: stop and restart the whole application, state comes back true."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cpm.api.app import create_app
from cpm.db.models import Project, Reservation, Run
from cpm.db.settings_repo import set_setting
from cpm.recovery.backup import list_backups
from cpm.security.secrets_store import MemorySecretStore
from cpm.security.tokens import csrf_for, load_or_create_token
from cpm.tasks.service import TaskService
from tests.fixtures.repos import make_repo


def boot(settings, **kw):
    return TestClient(
        create_app(settings, secret_store=MemorySecretStore()),
        base_url="http://127.0.0.1:8765",
        **kw,
    )


def headers(settings):
    t = load_or_create_token(settings.data_dir)
    return {"X-CPM-Token": t, "X-CPM-CSRF": csrf_for(t)}


def test_scenario_11_stop_during_a_task_then_restart(tmp_path):
    from cpm.config import Settings

    settings = Settings(
        host="127.0.0.1", port=8765, data_dir=tmp_path / "data", log_level="INFO",
        worktree_root=tmp_path / "wt",
    )  # fmt: skip
    repo = make_repo(tmp_path / "managed")
    with boot(settings) as c:
        eng = c.app.state.engine
        with Session(eng) as s:
            p = Project(slug="demo", name="Demo", path=str(repo))
            s.add(p)
            s.commit()
            pid = p.id
        set_setting(eng, "approved_roots", [str(repo)])
        tasks = TaskService(eng)
        t = tasks.mark_ready(tasks.create(pid, "long", billing_mode="mock").id, actor="t")
        c.app.state.supervisor.start(
            t.id, scenario="slow", scenario_args={"steps": 200, "delay_s": 0.1}, heartbeat_s=0.2
        )
        assert tasks.get(t.id).status == "RUNNING"
        # leave the `with`: the app shuts down while the worker is running
    with boot(settings) as c2:  # restart
        h = headers(settings)
        task = TaskService(c2.app.state.engine).get(t.id)
        assert task.status == "FAILED"  # not RUNNING, not lost, not silently completed
        rec = c2.get("/api/recovery", headers=h).json()
        assert rec["last_report"]["runs"] == []  # CPM shut down cleanly: nothing was orphaned
        assert len(rec["backups"]) >= 1 and len(list_backups(settings.data_dir)) >= 1
        with Session(c2.app.state.engine) as s:
            assert s.scalars(select(Reservation)).first() is None
            assert {r.status for r in s.scalars(select(Run))} <= {"KILLED"}


def test_scenario_18_crash_then_restart_restores_dashboard_and_state(tmp_path):
    from cpm.config import Settings
    from tests.recovery.conftest import RecEnv  # noqa: F401

    settings = Settings(
        host="127.0.0.1", port=8765, data_dir=tmp_path / "data", log_level="INFO",
        worktree_root=tmp_path / "wt",
    )  # fmt: skip
    repo = make_repo(tmp_path / "managed")
    with boot(settings) as c:
        eng = c.app.state.engine
        with Session(eng) as s:
            p = Project(slug="demo", name="Demo", path=str(repo))
            s.add(p)
            s.commit()
            pid = p.id
        tasks = TaskService(eng)
        done = tasks.mark_ready(tasks.create(pid, "queued", billing_mode="mock").id, actor="t")
        crashed = tasks.mark_ready(tasks.create(pid, "crashed", billing_mode="mock").id, actor="t")
        tasks.transition(crashed.id, "RESERVED", actor="t")
        tasks.transition(crashed.id, "RUNNING", actor="t")
        with Session(eng) as s:  # CPM "dies" here: run row says RUNNING, nobody is alive
            s.add(Run(task_id=crashed.id, attempt=1, billing_mode="mock", status="RUNNING"))
            s.commit()
    with boot(settings) as c2:
        h = headers(settings)
        dash = c2.get("/api/dashboard", headers=h).json()
        proj = next(p for p in dash["projects"] if p["name"] == "Demo")
        assert proj["task_counts"] == {"READY": 1, "FAILED": 1}
        assert dash["active_runs"] == []  # nothing claims to be running
        rep = c2.get("/api/recovery", headers=h).json()["last_report"]
        assert [o["task_id"] for o in rep["runs"]] == [crashed.id]
        tasks2 = TaskService(c2.app.state.engine)
        assert tasks2.get(crashed.id).status == "FAILED"
        assert tasks2.get(done.id).status == "READY"  # untouched
        assert c2.post("/api/recovery/reconcile", headers=h).json()["runs"] == []
        b = c2.post("/api/recovery/backup", headers=h).json()["file"]
        assert b in c2.get("/api/recovery", headers=h).json()["backups"]


def test_recovery_routes_need_token_and_csrf(client, auth):
    for m, path in (("get", "/api/recovery"), ("post", "/api/recovery/reconcile"),
                    ("post", "/api/recovery/backup")):  # fmt: skip
        assert getattr(client, m)(path).status_code == 401
    only_token = {"X-CPM-Token": auth["X-CPM-Token"]}
    assert client.post("/api/recovery/backup", headers=only_token).status_code in (400, 403)
    assert client.get("/api/recovery", headers=auth).status_code == 200
