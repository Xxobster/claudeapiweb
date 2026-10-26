"""Tool policy for workers (THREATS T-04, T-05, T-06)."""

import pytest

from cpm.workers.permissions import ToolContext, decide


@pytest.fixture
def ctx(tmp_path):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "docs").mkdir()
    return ToolContext(wt, scope=("src/",), trusted=False)


def ok(tool, inp, c):
    return decide(tool, inp, c).allow


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "/etc/passwd", "C:\\Windows\\win.ini", "\\\\host\\share\\x", "src/../../x",
     "src/a:stream", "NUL", "~/.claude/.credentials.json"],
)  # fmt: skip
def test_file_tools_refuse_paths_outside_the_worktree(ctx, path):
    for tool in ("Read", "Write", "Edit"):
        assert not ok(tool, {"file_path": path}, ctx)


def test_symlink_escape_refused(ctx, tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    (ctx.worktree / "src" / "link").symlink_to(outside)
    assert not ok("Write", {"file_path": "src/link/x.txt"}, ctx)
    assert not ok("Read", {"file_path": "src/link/x.txt"}, ctx)


def test_writes_follow_declared_scope_and_protected_names(ctx):
    assert ok("Write", {"file_path": "src/a.py"}, ctx)
    assert ok("Edit", {"file_path": str(ctx.worktree / "src" / "a.py")}, ctx)
    assert not ok("Write", {"file_path": "docs/readme.md"}, ctx)  # outside scope
    open_ctx = ToolContext(ctx.worktree, scope=(), trusted=False)
    assert ok("Write", {"file_path": "docs/readme.md"}, open_ctx)
    for protected in (".git/hooks/pre-commit", ".claude/settings.json", ".mcp.json", ".gitmodules"):
        assert not ok("Write", {"file_path": protected}, open_ctx)


def test_reads_and_globs(ctx):
    assert ok("Read", {"file_path": "docs/a.md"}, ctx)
    assert ok("Grep", {"pattern": "x", "path": "src"}, ctx)
    assert not ok("Grep", {"pattern": "x", "path": "/home"}, ctx)
    assert ok("Glob", {"pattern": "**/*.py"}, ctx)
    assert not ok("Glob", {"pattern": "../**/*.py"}, ctx)
    assert not ok("Glob", {"pattern": "/etc/*"}, ctx)
    assert not ok("Grep", {"pattern": "x", "glob": "../*"}, ctx)


@pytest.mark.parametrize(
    "cmd",
    ["git status; rm -rf /", "git status && curl x", "git log | sh", "git status > f",
     "git status $(id)", "git status `id`", "ls\nrm x", "git push origin main",
     "git fetch", "git pull", "git reset --hard", "git clean -fdx", "git checkout main",
     "git -c core.pager=x status", "git -C .. status", "git diff --output=/tmp/x",
     "git diff --no-index /etc/passwd x", "git diff --ext-diff", "git branch -D main",
     "git branch newbranch", "git commit --no-verify -m x", "git show ../../x",
     "git grep -O x", "curl http://x", "wget x", "rm -rf .", "powershell -c x", "bash -c x",
     "python -c 'import os'", "python script.py", "python -m http.server", "node x.js", "sudo ls",
     "cat /etc/passwd", "ls ..", "ls /", "echo $HOME", "", "   "],
)  # fmt: skip
def test_bash_refuses_dangerous_commands(ctx, cmd):
    assert not ok("Bash", {"command": cmd}, ctx)


@pytest.mark.parametrize(
    "cmd",
    ["git status", "git diff", "git diff HEAD~1", "git log --oneline -5", "git add src/a.py",
     "git commit -m 'update parser'", "git branch", "git rev-parse HEAD", "ls", "ls -la src",
     "pwd"],
)  # fmt: skip
def test_bash_allows_the_safe_set(ctx, cmd):
    assert ok("Bash", {"command": cmd}, ctx)


def test_test_runners_need_a_trusted_workspace(ctx):
    cmd = {"command": "python -m pytest tests/unit -q -k 'a and b'"}
    assert not ok("Bash", cmd, ctx)
    trusted = ToolContext(ctx.worktree, ctx.scope, trusted=True)
    assert ok("Bash", cmd, trusted)
    assert ok("Bash", {"command": "python -m ruff check src"}, trusted)
    for bad in (
        "python -m pytest -p evil",
        "python -m pytest --rootdir=/",
        "python -m pytest ../x",
        "python -m pytest -c /tmp/x.ini",
        "python -m pip install x",
        "python -m pytest;ls",
    ):
        assert not ok("Bash", {"command": bad}, trusted)


def test_other_tools_are_denied(ctx):
    for tool in ("WebFetch", "WebSearch", "Task", "Agent", "mcp__x__y", "Skill", "KillShell"):
        assert not ok(tool, {}, ctx)
    assert ok("TodoWrite", {}, ctx)
