"""Scheduler integration (lane resources make a task heavy), HTTP API, and the 0007 migration."""

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from cpm.db.models import Project
from cpm.db.settings_repo import set_setting
from cpm.locks.admission import Admission
from cpm.locks.reservations import ReservationService
from cpm.scheduler.core import Scheduler
from tests.fixtures.repos import make_repo
from tests.lanes.conftest import LANE, make_profile


def lane_task(e, title, **kw):
    t = e.tasks.create(e.pid, title, billing_mode="mock", lane="test", **kw)
    return e.tasks.mark_ready(t.id, actor="test")


def test_two_gpu_lane_tasks_never_run_together(lenv):
    res = ReservationService(
        lenv.engine, liveness=lambda t: lenv.sup.live_run_for_task(t) is not None
    )
    sched = Scheduler(
        lenv.engine, lenv.sup, Admission(lenv.engine, res), res,
        resources_for=lenv.lanes.resources,
        start_args_for=lambda t: {
            **lenv.lanes.start_args(t), "scenario": "slow",
            "scenario_args": {"steps": 8, "delay_s": 0.1}, "heartbeat_s": 0.2,
        },
    )  # fmt: skip
    a = lane_task(lenv, "a")
    b = lane_task(lenv, "b")
    sched.tick()
    states = {lenv.tasks.get(a.id).status, lenv.tasks.get(b.id).status}
    assert states == {"RUNNING", "READY"}, states  # one holds the gpu, the other waits
    for r in lenv.sup.list_runs(a.id) + lenv.sup.list_runs(b.id):
        lenv.sup.wait(r.id, 30)


@pytest.fixture
def api(client, auth, tmp_path, monkeypatch):
    app = client.app
    profile = make_profile()
    monkeypatch.setattr("cpm.profiles.load_profile", lambda _n: profile)
    app.state.lanes._load = lambda _n: profile
    repo = make_repo(tmp_path / "managed")
    with Session(app.state.engine) as s:
        p = Project(slug="demo", name="Demo", path=str(repo), profile="t")
        s.add(p)
        s.commit()
        pid = p.id
    set_setting(app.state.engine, "approved_roots", [str(repo)])
    client.pid = pid
    return client


def test_routes_need_token_and_csrf(client, auth):
    assert client.get("/api/lanes/validators").status_code in (401, 403)
    assert client.get("/api/projects/1/lanes").status_code in (401, 403)
    r = client.post(
        "/api/projects/1/lanes/test/tasks",
        json={"title": "x"},
        headers={"X-CPM-Token": auth["X-CPM-Token"]},
    )
    assert r.status_code in (401, 403)


def test_list_lanes_and_validators(api, auth):
    got = api.get(f"/api/projects/{api.pid}/lanes", headers=auth).json()["lanes"]
    assert [lane["id"] for lane in got] == ["test"]
    lane = got[0]
    assert lane["commands"][0]["program"] == "renderer" and lane["problems"] == []
    assert lane["artifacts"]["binary_policy"] == "ask"
    kinds = api.get("/api/lanes/validators", headers=auth).json()["validators"]
    assert {"files_exist", "file_size", "json_keys", "name_pattern"} <= set(kinds)


def test_create_task_from_the_lane_template(api, auth):
    url = f"/api/projects/{api.pid}/lanes/test/tasks"
    r = api.post(
        url,
        json={"title": "Scene 1", "inputs": {"scene": "intro.json"}, "billing_mode": "mock"},
        headers=auth,
    )
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["title"] == "[lane] Scene 1" and t["lane"] == "test" and t["status"] == "BACKLOG"
    assert t["declared_scope"] == ["assets/out/"] and t["acceptance"] == ["output exists"]
    assert "intro.json" in t["description"] and "not instructions" in t["description"]


@pytest.mark.parametrize(
    "body",
    [
        {"title": "x"},  # missing required input
        {"title": "x", "inputs": {"scene": "a", "extra": "b"}},  # input not in the template
        {"title": "x", "inputs": {"scene": "a" * 501}},
        {"title": "x", "inputs": {"scene": "a"}, "lane": "other"},  # unknown key
    ],
)
def test_template_inputs_are_checked(api, auth, body):
    r = api.post(f"/api/projects/{api.pid}/lanes/test/tasks", json=body, headers=auth)
    assert r.status_code == 422


def test_unknown_disabled_and_malformed_lanes(api, auth):
    base = f"/api/projects/{api.pid}/lanes"
    assert api.post(f"{base}/nope/tasks", json={"title": "x"}, headers=auth).status_code == 404
    assert api.post(f"{base}/Bad Name/tasks", json={"title": "x"}, headers=auth).status_code in (
        404,
        422,
    )
    api.app.state.lanes._load = lambda _n: make_profile(dict(LANE, enabled=False))
    r = api.post(f"{base}/test/tasks", json={"title": "x", "inputs": {"scene": "a"}}, headers=auth)
    assert r.status_code == 409


def test_task_api_accepts_a_known_lane_only(api, auth):
    ok = api.post(
        f"/api/projects/{api.pid}/tasks", json={"title": "t", "lane": "test"}, headers=auth
    )
    assert ok.status_code == 201 and ok.json()["lane"] == "test"
    bad = api.post(
        f"/api/projects/{api.pid}/tasks", json={"title": "t", "lane": "zzz"}, headers=auth
    )
    assert bad.status_code == 422
    assert (
        api.post(
            f"/api/projects/{api.pid}/tasks", json={"title": "t", "lane": "../x"}, headers=auth
        ).status_code
        == 422
    )


def test_migration_0007_adds_the_lane_column(client):
    insp = inspect(client.app.state.engine)
    assert "lane" in {c["name"] for c in insp.get_columns("tasks")}
    with client.app.state.engine.connect() as c:
        assert c.execute(text("select version_num from alembic_version")).scalar() == "0007"
