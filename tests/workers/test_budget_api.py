import time
from pathlib import Path

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

USD = 1_000_000


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
        set_setting(app.state.engine, "approved_roots", [str(repo)])
        with Session(app.state.engine) as s:
            p = Project(slug="demo", name="Demo", path=str(repo))
            s.add(p)
            s.commit()
            pid = p.id
        set_setting(
            app.state.engine, "wizard.config",
            {"global_budget_microusd": 10 * USD, "project_budget_microusd": 10 * USD,
             "task_budget_microusd": 2 * USD},
        )  # fmt: skip
        yield c, h, pid


def _ready_task(c, h, pid):
    tid = c.post(
        f"/api/projects/{pid}/tasks", json={"title": "T", "billing_mode": "mock"}, headers=h
    ).json()["id"]
    c.post(f"/api/tasks/{tid}/transition", json={"to": "READY"}, headers=h)
    return tid


def _run_to_budget_stop(c, h, tid):
    r = c.post(
        f"/api/tasks/{tid}/runs", headers=h,
        json={"scenario": "budget_burst", "scenario_args": {"step_microusd": 600_000},
              "max_budget_microusd": USD},
    )  # fmt: skip
    assert r.status_code == 201
    for _ in range(150):
        run = c.get(f"/api/runs/{r.json()['id']}", headers=h).json()
        if run["ended_at"]:
            return run
        time.sleep(0.1)
    raise AssertionError("no end")


def test_summary_prices_credit_and_reconcile(api):
    c, h, pid = api
    s = c.get("/api/budget", headers=h).json()
    assert s["month"]["cap_usd"] == "10" and s["month"]["spent_usd"] == "0"
    assert "estimate" in s["month"]["label"] and s["limitations"]
    prices = {p["model"]: p for p in c.get("/api/budget/prices", headers=h).json()}
    assert (
        prices["claude-sonnet-5-5"]["input_usd"] == "2"
        and prices["claude-sonnet-5-5"]["version"] == 1
    )
    put = c.put(
        "/api/budget/prices/claude-sonnet-5-5", headers=h,
        json={"input_usd": "2.5", "output_usd": "12", "cache_read_usd": "0.25",
              "cache_write_usd": "3", "source": "test", "verified_on": "2026-11-01"},
    )  # fmt: skip
    assert put.status_code == 200 and put.json()["version"] == 2
    bad = c.put(
        "/api/budget/prices/Bad!", headers=h,
        json={"input_usd": "1", "output_usd": "1", "cache_read_usd": "1", "cache_write_usd": "1",
              "source": "t", "verified_on": "2026-11-01"},
    )  # fmt: skip
    assert bad.status_code == 422
    assert (
        c.put("/api/budget/credit-balance", json={"balance_usd": "4.50"}, headers=h).status_code
        == 200
    )
    cb = c.get("/api/budget", headers=h).json()["credit_balance"]
    assert cb["estimated_remaining_usd"] == "4.5" and "estimate" in cb["label"]
    rec = c.post("/api/budget/reconcile", json={"actual_usd": "1.25"}, headers=h).json()
    assert rec["reconciliation"]["actual_usd"] == "1.25"
    assert "reconciled" in c.get("/api/budget", headers=h).json()["month"]["label"]


def test_money_endpoints_need_the_token_and_reject_junk(api):
    c, h, _ = api
    assert c.get("/api/budget").status_code == 401

    def topup(**body):
        return c.post("/api/budget/top-up", json=body, headers=h).status_code

    assert c.post("/api/budget/top-up", json={"scope": "month", "amount_usd": "1"}).status_code in (
        401,
        403,
    )
    assert topup(scope="month", amount_usd="-1") == 422
    assert topup(scope="task", ref="x", amount_usd="1") == 422
    assert topup(scope="month", amount_usd="99999") == 422
    assert c.get("/api/budget?period=2026-99", headers=h).status_code == 422


def test_budget_exhaustion_and_the_five_options(api):
    c, h, pid = api
    tid = _ready_task(c, h, pid)
    assert _run_to_budget_stop(c, h, tid)["status"] == "BUDGET_STOPPED"
    assert c.get(f"/api/tasks/{tid}", headers=h).json()["status"] == "PAUSED_BUDGET"
    tb = c.get(f"/api/tasks/{tid}/budget", headers=h).json()
    assert tb["spent_usd"] == "1.2" and tb["cap_usd"] == "2" and tb["remaining_usd"] == "0.8"

    # a run is refused with an explanation while the task has too little left
    r = c.post(f"/api/tasks/{tid}/runs", json={"max_budget_microusd": 2 * USD}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["scope"] == "task"

    act = lambda **kw: c.post(f"/api/tasks/{tid}/budget-action", json=kw, headers=h)  # noqa: E731
    assert act(action="resume_no_increase").json()["headroom_usd"] == "0.8"
    assert act(action="reassign_model", model="gpt-4").status_code == 422
    assert (
        act(action="reassign_model", model="claude-haiku-5-5").json()["model"] == "claude-haiku-5-5"
    )
    assert act(action="defer").json()["defer_until"].endswith("+00:00")
    assert act(action="add_budget").status_code == 422  # amount required
    assert act(action="add_budget", amount_usd="1.5").json()["cap_usd"] == "3.5"
    r = c.post(f"/api/tasks/{tid}/resume", json={"max_budget_microusd": 2 * USD}, headers=h)
    assert r.status_code == 201  # admission now passes
    for _ in range(100):
        if c.get(f"/api/runs/{r.json()['id']}", headers=h).json()["ended_at"]:
            break
        time.sleep(0.1)
    assert c.get(f"/api/tasks/{tid}", headers=h).json()["status"] == "TESTING"
    assert act(action="cancel").status_code == 409  # only a PAUSED_BUDGET task has these options


def test_cancel_option_cancels_and_keeps_the_worktree(api):
    c, h, pid = api
    tid = _ready_task(c, h, pid)
    _run_to_budget_stop(c, h, tid)
    r = c.post(f"/api/tasks/{tid}/budget-action", json={"action": "cancel"}, headers=h)
    assert r.status_code == 200
    assert c.get(f"/api/tasks/{tid}", headers=h).json()["status"] == "CANCELLED"
    wt = c.get(f"/api/tasks/{tid}/runs", headers=h).json()[0]["worktree_path"]
    assert Path(wt).is_dir()  # cancelling never removes the worktree


def test_top_up_then_resume_no_increase_reports_the_headroom(api):
    c, h, pid = api
    tid = _ready_task(c, h, pid)
    _run_to_budget_stop(c, h, tid)
    c.post(
        "/api/budget/top-up", json={"scope": "task", "ref": str(tid), "amount_usd": "1"}, headers=h
    )
    # simulate exhausted accounting: the task cap equals its spend after a reconcile-free check
    out = c.post(
        f"/api/tasks/{tid}/budget-action", json={"action": "resume_no_increase"}, headers=h
    )
    assert out.status_code == 200 and out.json()["headroom_usd"] == "1.8"
