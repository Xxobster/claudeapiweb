import pytest


def url(pid, tail=""):
    return f"/api/projects/{pid}/tasks{tail}"


def test_scenario_4_todo_to_tasks_via_api(client, auth, project_id):
    text = "# Goals\n- [ ] Build login\n  - [ ] add form\n- [x] done thing\n"
    r = client.post(url(project_id, "/import-todo"), json={"text": text}, headers=auth)
    assert r.status_code == 201, r.text
    body = r.json()
    assert [t["title"] for t in body["created"]] == ["Build login", "add form"]
    assert body["skipped_done"] == 1
    listing = client.get(url(project_id), headers=auth).json()
    assert len(listing) == 2 and listing[1]["parent_id"] == listing[0]["id"]
    again = client.post(url(project_id, "/import-todo"), json={"text": text}, headers=auth).json()
    assert again["created"] == [] and again["skipped_existing"] == 2


def test_crud_and_lifecycle(client, auth, project_id):
    a = client.post(url(project_id), json={"title": "a", "declared_scope": ["src/"]}, headers=auth)
    assert a.status_code == 201
    a = a.json()
    b = client.post(
        url(project_id), json={"title": "b", "depends_on": [a["id"]]}, headers=auth
    ).json()
    assert (
        client.post(f"/api/tasks/{b['id']}/transition", json={"to": "READY"}, headers=auth).json()[
            "status"
        ]
        == "BLOCKED"
    )
    r = client.post(f"/api/tasks/{a['id']}/transition", json={"to": "READY"}, headers=auth)
    assert r.json()["status"] == "READY"
    q = client.get(f"/api/projects/{project_id}/ready-queue", headers=auth).json()
    assert [t["id"] for t in q] == [a["id"]]
    p = client.patch(f"/api/tasks/{a['id']}", json={"priority": 1, "title": "A!"}, headers=auth)
    assert p.json()["priority"] == 1 and p.json()["title"] == "A!"
    d = client.get(f"/api/tasks/{a['id']}", headers=auth).json()
    assert d["dependents"] == [b["id"]] and d["events"]
    assert client.post(f"/api/tasks/{a['id']}/cancel", headers=auth).json()["status"] == "CANCELLED"
    assert client.get(f"/api/tasks/{b['id']}", headers=auth).json()["status"] == "BLOCKED"


def test_cycle_and_illegal_transition_codes(client, auth, project_id):
    a = client.post(url(project_id), json={"title": "a"}, headers=auth).json()
    b = client.post(
        url(project_id), json={"title": "b", "depends_on": [a["id"]]}, headers=auth
    ).json()
    r = client.post(
        f"/api/tasks/{a['id']}/dependencies", json={"depends_on_id": b["id"]}, headers=auth
    )
    assert r.status_code == 409
    r = client.post(f"/api/tasks/{a['id']}/transition", json={"to": "CANCELLED"}, headers=auth)
    assert r.status_code == 200
    r = client.post(f"/api/tasks/{a['id']}/transition", json={"to": "READY"}, headers=auth)
    assert r.status_code == 409


@pytest.mark.parametrize(
    "to",
    [
        "RESERVED",
        "RUNNING",
        "TESTING",
        "REVIEWING",
        "CORRECTING",
        "PAUSED_BUDGET",
        "FAILED",
        "COMPLETED",
        "INTEGRATED",
    ],
)
def test_humans_cannot_set_machine_states(client, auth, project_id, to):
    a = client.post(url(project_id), json={"title": "a"}, headers=auth).json()
    r = client.post(f"/api/tasks/{a['id']}/transition", json={"to": to}, headers=auth)
    assert r.status_code == 409
    assert client.get(f"/api/tasks/{a['id']}", headers=auth).json()["status"] == "BACKLOG"


def test_split_merge_api(client, auth, project_id):
    a = client.post(url(project_id), json={"title": "a"}, headers=auth).json()
    parts = client.post(
        f"/api/tasks/{a['id']}/split",
        json={"parts": [{"title": "p1"}, {"title": "p2"}]},
        headers=auth,
    )
    assert parts.status_code == 201
    ids = [p["id"] for p in parts.json()]
    m = client.post(url(project_id, "/merge"), json={"task_ids": ids, "title": "m"}, headers=auth)
    assert m.status_code == 201 and m.json()["title"] == "m"


def test_plan_mock_and_live_refused(client, auth, project_id):
    r = client.post(url(project_id, "/plan"), json={"prompt": "- one\n- two"}, headers=auth)
    assert r.status_code == 201 and r.json()["planner"] == "mock"
    assert [t["source"] for t in r.json()["created"]] == ["prompt", "prompt"]
    r = client.post(url(project_id, "/plan"), json={"prompt": "x", "live": True}, headers=auth)
    assert r.status_code == 501


def test_unknown_project_and_task(client, auth):
    assert client.post(url(999), json={"title": "x"}, headers=auth).status_code == 404
    assert client.get("/api/tasks/999", headers=auth).status_code == 404


def test_states_endpoint(client, auth):
    body = client.get("/api/task-states", headers=auth).json()
    assert len(body["statuses"]) == 14 and "COMPLETED" not in body["user_targets"]


def test_prompts_api(client, auth):
    assert (
        client.post("/api/prompts", json={"name": "p", "body": "hello"}, headers=auth).json()[
            "version"
        ]
        == 1
    )
    client.post("/api/prompts", json={"name": "p", "body": "hello2"}, headers=auth)
    assert client.get("/api/prompts/p", headers=auth).json()["body"] == "hello2"
    assert client.get("/api/prompts/p?version=1", headers=auth).json()["body"] == "hello"
    assert [p["name"] for p in client.get("/api/prompts", headers=auth).json()] == ["p"]


@pytest.mark.parametrize(
    "field",
    [
        "title",
        "description",
        "priority",
        "billing_mode",
        "declared_scope",
        "acceptance",
        "estimate_microusd",
    ],
)
def test_patch_null_on_non_nullable_is_422(client, auth, project_id, field):
    a = client.post(url(project_id), json={"title": "a"}, headers=auth).json()
    r = client.patch(f"/api/tasks/{a['id']}", json={field: None}, headers=auth)
    assert r.status_code == 422
    assert (
        client.patch(
            f"/api/tasks/{a['id']}", json={"role": None, "model": None}, headers=auth
        ).status_code
        == 200
    )


def test_task_model_and_effort_choice(client, auth, project_id):
    r = client.post(
        url(project_id),
        json={"title": "deep", "model": "claude-fable-5-1", "effort": "xhigh"},
        headers=auth,
    )
    assert r.status_code == 201, r.text
    assert (r.json()["model"], r.json()["effort"]) == ("claude-fable-5-1", "xhigh")
    r2 = client.post(
        url(project_id),
        json={"title": "light", "model": "claude-sonnet-5-5", "effort": "low"},
        headers=auth,
    )
    assert (r2.json()["model"], r2.json()["effort"]) == ("claude-sonnet-5-5", "low")
    for bad in ({"model": "gpt-4"}, {"effort": "ultra"}):
        assert (
            client.post(url(project_id), json={"title": "x", **bad}, headers=auth).status_code
            == 422
        )
    tid = r2.json()["id"]
    assert (
        client.patch(f"/api/tasks/{tid}", json={"model": "nope"}, headers=auth).status_code == 422
    )
    ok = client.patch(f"/api/tasks/{tid}", json={"model": "claude-haiku-5-5"}, headers=auth)
    assert ok.status_code == 200 and ok.json()["model"] == "claude-haiku-5-5"


def test_rough_estimate_follows_model_and_effort(client, auth):
    def est(model, effort):
        r = client.get(f"/api/budget/estimate?model={model}&effort={effort}", headers=auth)
        assert r.status_code == 200, r.text
        return r.json()["estimate_microusd"]

    assert (
        est("claude-fable-5-1", "high")
        > est("claude-sonnet-5-5", "high")
        > est("claude-haiku-5-5", "high")
    )
    assert est("claude-sonnet-5-5", "max") > est("claude-sonnet-5-5", "low")
    assert (
        client.get("/api/budget/estimate?model=gpt-4&effort=low", headers=auth).status_code == 422
    )
    assert (
        client.get(
            "/api/budget/estimate?model=claude-sonnet-5-5&effort=zz", headers=auth
        ).status_code
        == 422
    )
