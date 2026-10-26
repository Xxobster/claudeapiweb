"""WP4 security checks: scope path traversal, secrets in prompts/events, guard coverage."""

import json

import pytest

from cpm.tasks.service import InvalidTask

BAD_SCOPES = [
    "../x", "a/../../x", "/etc/passwd", "C:\\Windows", "C:/x", "\\\\server\\share", "a\x00b",
    "CON", "dir/NUL.txt", "a:b", "trail.", " lead", "",
]  # fmt: skip


@pytest.mark.parametrize("scope", BAD_SCOPES)
def test_declared_scope_rejects_unsafe_paths(svc, scope):
    with pytest.raises(InvalidTask):
        svc.create(svc.pid, "t", declared_scope=[scope])


def test_scope_normalised_and_deduplicated(svc):
    t = svc.create(svc.pid, "t", declared_scope=["src\\a\\", "src/a/", "docs/x.md"])
    assert t.declared_scope == ["src/a/", "docs/x.md"]


def test_scope_rejected_over_api_without_echoing_input(client, auth, project_id):
    r = client.post(
        f"/api/projects/{project_id}/tasks",
        json={"title": "t", "declared_scope": ["../../secret"]},
        headers=auth,
    )
    assert r.status_code == 422


def test_task_routes_need_token_and_csrf(client, auth, project_id):
    u = f"/api/projects/{project_id}/tasks"
    assert client.get(u).status_code == 401
    assert client.post(u, json={"title": "x"}).status_code == 401
    only_token = {"X-CPM-Token": auth["X-CPM-Token"]}
    assert client.post(u, json={"title": "x"}, headers=only_token).status_code == 403
    assert client.get(u, headers=only_token).status_code == 200
    bad_origin = {**auth, "Origin": "http://evil.example"}
    assert client.post(u, json={"title": "x"}, headers=bad_origin).status_code == 403
    for tail in ("/prompts", "/task-states", "/tasks/1"):
        assert client.get("/api" + tail).status_code == 401


def test_events_never_store_secrets(svc):
    t = svc.create(svc.pid, "deploy with sk-ant-api03-ABCDEFGHIJKL", description="ghp_" + "z" * 30)
    svc.cancel(t.id, actor="u", reason="api_key: abcdef123456 leaked")
    blob = json.dumps(svc.events(t.id), default=str)
    assert "ABCDEFGHIJKL" not in blob and "abcdef123456" not in blob


def test_validation_errors_do_not_echo_secret_input(client, auth, project_id):
    r = client.post(
        "/api/prompts", json={"name": "x" * 500, "body": "sk-ant-api03-SECRETSECRET"}, headers=auth
    )
    assert r.status_code == 422 and "SECRETSECRET" not in r.text
    r = client.post(
        "/api/prompts", json={"name": "ok", "body": "sk-ant-api03-SECRETSECRET"}, headers=auth
    )
    assert r.status_code == 422 and "SECRETSECRET" not in r.text


def test_sql_injection_strings_are_inert(svc):
    t = svc.create(svc.pid, "x'); DROP TABLE tasks;--")
    assert svc.get(t.id).title.startswith("x'); DROP")
    assert len(svc.list_tasks(svc.pid)) == 1


def test_no_shell_use_in_tasks_package():
    import pathlib

    for f in pathlib.Path("src/cpm/tasks").glob("*.py"):
        text = f.read_text()
        assert "subprocess" not in text and "os.system" not in text, f
