"""Gate / diff / integration endpoints and the GitHub token credential."""

import pytest
from sqlalchemy.orm import Session

from cpm.db.models import Project
from cpm.db.settings_repo import set_setting
from cpm.review.github import GITHUB_TOKEN
from cpm.review.reviewer import MockReviewer, ReviewerSource
from cpm.review.testrun import TestOutcome
from cpm.workers.worktree import WorktreeManager
from tests.fixtures.repos import make_repo

TOKEN = "github_pat_" + "k9" * 20


class Pass:
    __test__ = False

    def run(self, argv, cwd, timeout_s):
        return TestOutcome(list(argv), 0, False, 0.0, "ok")


@pytest.fixture
def app_env(client, auth, tmp_path):
    app = client.app
    app.state.supervisor.worktrees = WorktreeManager(tmp_path / "wt")
    app.state.gate.worktrees = app.state.supervisor.worktrees
    app.state.integration.worktrees = app.state.supervisor.worktrees
    app.state.gate.runner = Pass()
    app.state.gate.reviewers = ReviewerSource(mock=MockReviewer())
    repo = make_repo(tmp_path / "managed")
    eng = app.state.engine
    with Session(eng) as s:
        p = Project(slug="demo", name="Demo", path=str(repo))
        s.add(p)
        s.commit()
        pid = p.id
    set_setting(eng, "approved_roots", [str(repo)])
    tasks = app.state.scheduler.tasks
    t = tasks.create(pid, "t", billing_mode="mock", acceptance=["it exists"])
    tasks.mark_ready(t.id, actor="test")
    sup = app.state.supervisor
    run = sup.start(t.id, heartbeat_s=0.2)
    sup.wait(run.id, 30)
    return t.id


def test_routes_need_token_and_csrf(client, auth):
    for method, path in (
        ("get", "/api/tasks/1/review"), ("get", "/api/tasks/1/changes"),
        ("get", "/api/tasks/1/diff"),
        ("post", "/api/tasks/1/gate"), ("post", "/api/tasks/1/gate/approve"),
        ("post", "/api/tasks/1/integrate"), ("get", "/api/gate/config"),
        ("put", "/api/gate/config"),
        ("get", "/api/credentials/github"), ("put", "/api/credentials/github"),
    ):  # fmt: skip
        assert getattr(client, method)(path).status_code == 401, path
    only_token = {"X-CPM-Token": auth["X-CPM-Token"]}
    assert client.post("/api/tasks/1/gate", headers=only_token).status_code in (400, 403)
    assert client.post("/api/tasks/1/integrate", headers=only_token).status_code in (400, 403)


def test_changes_diff_gate_flow(client, auth, app_env):
    tid = app_env
    ch = client.get(f"/api/tasks/{tid}/changes", headers=auth).json()
    assert [f["path"] for f in ch["files"]] == [f"mock_output_task{tid}.txt"]
    d = client.get(
        f"/api/tasks/{tid}/diff", params={"path": ch["files"][0]["path"]}, headers=auth
    ).json()
    assert "+mock result" in d["diff"]
    assert (
        client.get(f"/api/tasks/{tid}/diff", params={"path": "../../x"}, headers=auth).status_code
        == 409
    )
    out = client.post(f"/api/tasks/{tid}/gate", headers=auth).json()
    assert out["outcome"] == "passed" and out["task_status"] == "COMPLETED"
    rv = client.get(f"/api/tasks/{tid}/review", headers=auth).json()
    assert (
        rv["results"][0]["outcome"] == "passed"
        and rv["integration"] is None
        and rv["running"] is False
    )
    assert (
        client.post(f"/api/tasks/{tid}/gate", headers=auth).status_code == 409
    )  # no longer TESTING


def test_nobody_can_complete_a_task_through_the_transition_api(client, auth, app_env):
    r = client.post(f"/api/tasks/{app_env}/transition", headers=auth, json={"to": "COMPLETED"})
    assert r.status_code in (409, 422)
    assert client.get(f"/api/tasks/{app_env}", headers=auth).json()["status"] == "TESTING"


def test_integrate_without_token_is_a_clear_409(client, auth, app_env):
    client.post(f"/api/tasks/{app_env}/gate", headers=auth)
    r = client.post(f"/api/tasks/{app_env}/integrate", headers=auth, json={"needs_pc": "x"})
    assert r.status_code == 409 and (
        "GitHub" in r.json()["detail"] or "remote" in r.json()["detail"]
    )


def test_gate_config_roundtrip_and_validation(client, auth):
    assert client.get("/api/gate/config", headers=auth).json() == {
        "max_corrections": 2,
        "auto": True,
    }
    r = client.put("/api/gate/config", headers=auth, json={"max_corrections": 1, "auto": False})
    assert r.json() == {"max_corrections": 1, "auto": False}
    assert (
        client.put("/api/gate/config", headers=auth, json={"max_corrections": 9}).status_code == 422
    )


def test_github_token_is_stored_validated_and_never_returned(client, auth, store):
    assert client.get("/api/credentials/github", headers=auth).json()["configured"] is False
    bad = client.put("/api/credentials/github", headers=auth, json={"token": "hunter2"})
    assert bad.status_code == 422 and "hunter2" not in bad.text
    ok = client.put("/api/credentials/github", headers=auth, json={"token": TOKEN})
    assert ok.json() == {"configured": True, "storage_available": True}
    assert TOKEN not in ok.text and store.get(GITHUB_TOKEN) == TOKEN
    assert TOKEN not in client.get("/api/credentials/github", headers=auth).text
    assert client.delete("/api/credentials/github", headers=auth).json()["configured"] is False
    assert store.get(GITHUB_TOKEN) is None


def test_validation_errors_do_not_echo_the_token(client, auth):
    r = client.put("/api/credentials/github", headers=auth, json={"nottoken": TOKEN})
    assert r.status_code == 422 and TOKEN not in r.text
