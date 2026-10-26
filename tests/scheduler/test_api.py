import pytest

from cpm.config import ConfigError, load_settings


def test_scheduler_routes_need_the_token(client):
    for method, path in (
        ("get", "/api/scheduler"), ("post", "/api/scheduler/pause"),
        ("post", "/api/scheduler/tick"), ("put", "/api/scheduler/config"),
    ):  # fmt: skip
        assert getattr(client, method)(path).status_code == 401


def test_mutations_need_csrf(client, auth):
    only_token = {"X-CPM-Token": auth["X-CPM-Token"]}
    assert client.post("/api/scheduler/pause", headers=only_token).status_code in (400, 403)


def test_status_pause_resume_tick(client, auth):
    st = client.get("/api/scheduler", headers=auth).json()
    assert st["enabled"] is False and st["paused"] is False and st["limits"]["heavy"] == 1
    assert client.post("/api/scheduler/pause", headers=auth).json() == {"paused": True}
    assert client.get("/api/scheduler", headers=auth).json()["paused"] is True
    tick = client.post("/api/scheduler/tick", headers=auth).json()
    assert tick["paused"] is True and tick["started"] == []
    assert client.post("/api/scheduler/resume", headers=auth).json() == {"paused": False}
    assert client.get("/api/scheduler", headers=auth).json()["last_tick"] is not None


def test_manual_tick_starts_a_ready_mock_task(client, auth, settings, tmp_path):
    from sqlalchemy.orm import Session

    from cpm.db.models import Project
    from cpm.db.settings_repo import set_setting
    from cpm.workers.worktree import WorktreeManager
    from tests.fixtures.repos import make_repo

    client.app.state.supervisor.worktrees = WorktreeManager(tmp_path / "wt")
    repo = make_repo(tmp_path / "managed")
    eng = client.app.state.engine
    with Session(eng) as s:
        p = Project(slug="demo", name="Demo", path=str(repo))
        s.add(p)
        s.commit()
        pid = p.id
    set_setting(eng, "approved_roots", [str(repo)])
    t = client.app.state.scheduler.tasks.create(pid, "t", billing_mode="mock")
    client.app.state.scheduler.tasks.mark_ready(t.id, actor="test")
    out = client.post("/api/scheduler/tick", headers=auth).json()
    assert out["started"] == [t.id], out
    sup = client.app.state.supervisor
    for r in sup.list_runs(t.id):
        sup.wait(r.id, 30)


def test_config_is_validated_and_stored(client, auth):
    r = client.put("/api/scheduler/config", headers=auth, json={"max_auto_retries": 1})
    assert r.status_code == 200 and r.json()["max_auto_retries"] == 1
    assert client.get("/api/scheduler", headers=auth).json()["config"]["max_auto_retries"] == 1
    for bad in ({"max_auto_retries": 99}, {"evil": 1}, {"retry_billing_modes": ["root"]}):
        assert client.put("/api/scheduler/config", headers=auth, json=bad).status_code == 422


def test_env_flags(tmp_path):
    base = {"CPM_DATA_DIR": str(tmp_path / "d")}
    assert load_settings(base).scheduler_enabled is False
    on = load_settings({**base, "CPM_SCHEDULER": "1", "CPM_SCHEDULER_INTERVAL_S": "0.5"})
    assert on.scheduler_enabled and on.scheduler_interval_s == 0.5
    for bad in ("abc", "0", "9999"):
        with pytest.raises(ConfigError):
            load_settings({**base, "CPM_SCHEDULER_INTERVAL_S": bad})


def test_loop_starts_with_the_app_when_enabled(settings):
    import dataclasses

    from fastapi.testclient import TestClient

    from cpm.api.app import create_app
    from cpm.security.secrets_store import MemorySecretStore

    s = dataclasses.replace(settings, scheduler_enabled=True, scheduler_interval_s=0.2)
    with TestClient(
        create_app(s, secret_store=MemorySecretStore()), base_url="http://127.0.0.1:8765"
    ) as c:
        import time

        for _ in range(50):
            if c.app.state.scheduler._last is not None:
                break
            time.sleep(0.1)
        assert c.app.state.scheduler._last is not None
