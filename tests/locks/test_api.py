from cpm.locks.types import ResourceRequest
from tests.fixtures.repos import make_repo, write


def _project(client, tmp_path, auth):
    repo = make_repo(tmp_path / "managed")
    # approve the folder the way a user does
    r = client.post("/api/projects", json={"name": "m", "path": str(repo)}, headers=auth)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"], repo


def test_claim_flow_and_conflict_explanation(client, auth, settings, tmp_path):
    pid, _ = _project(client, tmp_path, auth)
    r = client.post(
        f"/api/projects/{pid}/desktop-claims",
        json={"path": "Assets/Game/UI/", "note": "menu"}, headers=auth,
    )  # fmt: skip
    assert r.status_code == 201
    cid = r.json()["id"]
    t = client.post(
        f"/api/projects/{pid}/tasks",
        json={"title": "ui", "declared_scope": ["Assets/Game/UI/menu.prefab"]}, headers=auth,
    ).json()["id"]  # fmt: skip
    conflicts = client.get(f"/api/tasks/{t}/conflicts", headers=auth).json()
    assert conflicts["blockers"][0]["message"].startswith(
        "blocked by: Desktop claim on `Assets/Game/UI/`"
    )
    assert (
        len(client.get(f"/api/projects/{pid}/desktop-claims", headers=auth).json()["claims"]) == 1
    )
    assert client.delete(f"/api/desktop-claims/{cid}", headers=auth).status_code == 200
    assert client.get(f"/api/tasks/{t}/conflicts", headers=auth).json()["blockers"] == []
    assert client.get(f"/api/projects/{pid}/desktop-claims", headers=auth).json()["claims"] == []


def test_claim_rejects_traversal(client, auth, tmp_path):
    pid, _ = _project(client, tmp_path, auth)
    r = client.post(f"/api/projects/{pid}/desktop-claims", json={"path": "../x"}, headers=auth)
    assert r.status_code == 422
    assert (
        client.post(
            "/api/projects/999/desktop-claims", json={"path": "a/"}, headers=auth
        ).status_code
        == 404
    )
    assert client.delete("/api/desktop-claims/999", headers=auth).status_code == 404


def test_reservations_listing_and_manual_release(client, auth, tmp_path):
    pid, _ = _project(client, tmp_path, auth)
    t = client.post(f"/api/projects/{pid}/tasks", json={"title": "x"}, headers=auth).json()["id"]
    client.app.state.reservations.acquire(t, pid, [ResourceRequest("path", "a/", "excl", "a/")])
    rows = client.get(f"/api/projects/{pid}/reservations", headers=auth).json()["reservations"]
    assert [r["key"] for r in rows] == ["a/"]
    assert client.post(f"/api/tasks/{t}/reservations/release", headers=auth).json() == {
        "released": 1
    }
    assert client.post("/api/tasks/9999/reservations/release", headers=auth).status_code == 404


def test_external_scan_and_locks_endpoints(client, auth, tmp_path):
    pid, repo = _project(client, tmp_path, auth)
    assert (
        client.post(f"/api/projects/{pid}/external-scan", headers=auth).json()["baseline"] is True
    )
    write(repo, "x.txt")
    res = client.post(f"/api/projects/{pid}/external-scan", headers=auth).json()
    assert res["changed"] == ["x.txt"]
    assert client.get(f"/api/projects/{pid}/locks", headers=auth).json() == {
        "locks": []
    }  # no profile
    assert client.post("/api/projects/999/external-scan", headers=auth).status_code == 404


def test_job_board_routes_need_a_profile_and_never_leak(client, auth, tmp_path):
    pid, _ = _project(client, tmp_path, auth)
    t = client.post(
        f"/api/projects/{pid}/tasks", json={"title": "ui", "declared_scope": ["Assets/UI/"]},
        headers=auth,
    ).json()["id"]  # fmt: skip
    st = client.get(f"/api/tasks/{t}/job-board", headers=auth)
    assert st.status_code == 200 and st.json()["claim"] is None and st.json()["problem"]
    for action in ("claim", "release"):
        r = client.post(f"/api/tasks/{t}/job-board/{action}", headers=auth)
        assert r.status_code == 409, r.text
    assert client.get("/api/tasks/9999/job-board", headers=auth).status_code == 404
    # token and CSRF are required like everywhere else
    assert client.get(f"/api/tasks/{t}/job-board").status_code in (401, 403)
    assert client.post(f"/api/tasks/{t}/job-board/claim").status_code in (401, 403)
