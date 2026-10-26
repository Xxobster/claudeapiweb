import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cpm.api.app import create_app
from cpm.config import Settings
from cpm.db.models import Project
from cpm.db.settings_repo import set_setting
from cpm.security.secrets_store import MemorySecretStore
from cpm.security.tokens import csrf_for, load_or_create_token
from tests.fixtures.repos import make_repo


@pytest.fixture
def api(tmp_path):
    settings = Settings(
        host="127.0.0.1", port=8765, data_dir=tmp_path / "data", log_level="INFO",
        worktree_root=tmp_path / "wt",
    )  # fmt: skip
    app = create_app(settings, secret_store=MemorySecretStore())
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        token = load_or_create_token(settings.data_dir)
        h = {"X-CPM-Token": token, "X-CPM-CSRF": csrf_for(token)}
        repo = make_repo(tmp_path / "repo")
        with Session(app.state.engine) as s:
            p = Project(slug="demo", name="Demo", path=str(repo))
            s.add(p)
            s.commit()
            pid = p.id
        set_setting(app.state.engine, "approved_roots", [str(repo)])
        yield c, h, pid, token


def _task(c, h, pid, **kw):
    t = c.post(
        f"/api/projects/{pid}/tasks", json={"title": "T", "billing_mode": "mock", **kw}, headers=h
    )
    assert t.status_code == 201
    tid = t.json()["id"]
    assert (
        c.post(f"/api/tasks/{tid}/transition", json={"to": "READY"}, headers=h).status_code == 200
    )
    return tid


def _wait_ended(c, h, rid):
    for _ in range(150):
        r = c.get(f"/api/runs/{rid}", headers=h).json()
        if r["ended_at"]:
            return r
        time.sleep(0.1)
    raise AssertionError("run did not end")


def test_start_poll_events_and_delete_worktree(api):
    c, h, pid, _ = api
    tid = _task(c, h, pid)
    r = c.post(f"/api/tasks/{tid}/runs", json={}, headers=h)
    assert r.status_code == 201 and r.json()["billing_mode"] == "mock"
    done = _wait_ended(c, h, r.json()["id"])
    assert done["status"] == "SUCCEEDED"
    ev = c.get(f"/api/runs/{done['id']}/events", headers=h).json()
    assert ev["live"] is False and ev["events"][-1]["type"] == "run_ended"
    assert (
        c.get(f"/api/runs/{done['id']}/events?since={ev['events'][-1]['seq']}", headers=h).json()[
            "events"
        ]
        == []
    )
    assert c.get(f"/api/tasks/{tid}/runs", headers=h).json()[0]["id"] == done["id"]
    # the worker left a file in its worktree: deletion is refused
    assert c.delete(f"/api/tasks/{tid}/worktree", headers=h).status_code == 409


def test_cancel_through_the_task_api_stops_the_worker(api):
    c, h, pid, _ = api
    tid = _task(c, h, pid)
    r = c.post(
        f"/api/tasks/{tid}/runs",
        json={"scenario": "slow", "scenario_args": {"steps": 1000}},
        headers=h,
    )
    rid = r.json()["id"]
    out = c.post(f"/api/tasks/{tid}/cancel", headers=h)
    assert out.status_code == 200 and out.json()["status"] == "CANCELLED"
    assert _wait_ended(c, h, rid)["status"] == "CANCELLED"


def test_pause_resume_endpoints(api):
    c, h, pid, _ = api
    tid = _task(c, h, pid)
    rid = c.post(
        f"/api/tasks/{tid}/runs",
        json={"scenario": "slow", "scenario_args": {"steps": 1000}},
        headers=h,
    ).json()["id"]
    time.sleep(0.4)
    assert (
        c.post(f"/api/tasks/{tid}/transition", json={"to": "PAUSED"}, headers=h).json()["status"]
        == "PAUSED"
    )
    assert _wait_ended(c, h, rid)["status"] == "STOPPED"
    r2 = c.post(f"/api/tasks/{tid}/resume", json={}, headers=h)
    assert r2.status_code == 201 and r2.json()["attempt"] == 2
    assert _wait_ended(c, h, r2.json()["id"])["status"] == "SUCCEEDED"


def test_errors_map_to_http_codes(api):
    c, h, pid, _ = api
    assert c.post("/api/tasks/9999/runs", json={}, headers=h).status_code == 404
    tid = _task(c, h, pid)
    assert c.post(f"/api/tasks/{tid}/runs", json={"scenario": "nope"}, headers=h).status_code == 422
    assert c.post("/api/runs/9999/stop", headers=h).status_code == 404
    assert c.get("/api/runs/9999", headers=h).status_code == 404
    live = c.post(
        f"/api/tasks/{_task(c, h, pid, billing_mode='api')}/runs",
        json={"max_budget_microusd": 1000},
        headers=h,
    )
    assert live.status_code == 403  # CPM_ALLOW_LIVE_RUNS is off


def test_endpoints_are_guarded(api):
    c, h, pid, _ = api
    tid = _task(c, h, pid)
    assert c.post(f"/api/tasks/{tid}/runs", json={}).status_code == 401
    assert (
        c.post(
            f"/api/tasks/{tid}/runs", json={}, headers={"X-CPM-Token": h["X-CPM-Token"]}
        ).status_code
        == 403
    )
    assert c.get("/api/runs/1/events").status_code == 401


def test_websocket_streams_and_replays(api):
    c, h, pid, token = api
    tid = _task(c, h, pid)
    rid = c.post(
        f"/api/tasks/{tid}/runs",
        json={"scenario": "slow", "scenario_args": {"steps": 5, "delay_s": 0.1}},
        headers=h,
    ).json()["id"]
    seen = []
    with c.websocket_connect(
        f"/api/runs/{rid}/stream",
        headers={"X-CPM-Token": token, "Origin": "http://127.0.0.1:8765", "Host": "127.0.0.1:8765"},
    ) as ws:
        while True:
            m = ws.receive_json()
            seen.append(m)
            if m["type"] == "end":
                break
    types = [m["type"] for m in seen]
    assert types[0] == "run_created" and "result" in types and types[-2] == "run_ended"
    seqs = [m["seq"] for m in seen if m["type"] != "end"]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    with c.websocket_connect(
        f"/api/runs/{rid}/stream?since={seqs[-2]}",
        headers={"X-CPM-Token": token, "Origin": "http://127.0.0.1:8765", "Host": "127.0.0.1:8765"},
    ) as ws:
        assert ws.receive_json()["seq"] == seqs[-1]


def test_websocket_refuses_bad_origin_and_missing_token(api):
    c, h, pid, token = api
    from starlette.websockets import WebSocketDisconnect

    for headers in (
        {"X-CPM-Token": token, "Origin": "http://evil.example", "Host": "127.0.0.1:8765"},
        {"Origin": "http://127.0.0.1:8765", "Host": "127.0.0.1:8765"},
    ):
        with (
            pytest.raises(WebSocketDisconnect),
            c.websocket_connect("/api/runs/1/stream", headers=headers),
        ):
            pass
