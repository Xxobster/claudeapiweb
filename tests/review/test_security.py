"""Security checks for WP10 (docs/THREATS.md T-30..T-34): token handling, git safety, paths."""

import re
import sqlite3
import subprocess  # noqa: S404
from pathlib import Path

import pytest

from cpm.review import gitops
from tests.review.conftest import TOKEN

SRC = Path(__file__).resolve().parents[2] / "src" / "cpm" / "review"
FORBIDDEN_GIT = (
    "merge", "rebase", "reset", "clean", "stash", "checkout", "switch", "restore", "revert",
    "cherry-pick", "gc", "prune", "filter-branch", "--force", "--force-with-lease", "-f",
    "--mirror", "--delete", "--prune", "-D", "--hard",
)  # fmt: skip


def test_review_code_never_names_a_destructive_git_command():
    """Master prompt section 13: no merge, force-push, reset, clean or discard. (merge-base and
    merge-tree are read-only plumbing and are different words.)"""
    for f in SRC.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        for word in FORBIDDEN_GIT:
            assert not re.search(rf'["\']{re.escape(word)}["\']', text), f"{f.name}: {word}"
        assert "shell=True" not in text and "os.system" not in text


def test_pushed_refspec_has_no_force_marker_and_token_is_not_in_argv(ienv, monkeypatch):
    seen = []
    real = subprocess.run

    def spy(cmd, **kw):
        seen.append((list(cmd), dict(kw.get("env") or {})))
        return real(cmd, **kw)

    monkeypatch.setattr(gitops.subprocess, "run", spy)
    tid = ienv.completed()
    ienv.integ.integrate(tid)
    pushes = [(c, e) for c, e in seen if "push" in c]
    assert len(pushes) == 1
    argv, env = pushes[0]
    assert all(not a.startswith("+") for a in argv) and "--force" not in argv
    assert any(a.startswith("refs/heads/claude/") and ":" in a for a in argv)
    assert not any(TOKEN in a for c, _e in seen for a in c)  # never on a command line
    assert "refs/heads/main" not in " ".join(argv)


def test_token_header_goes_through_the_environment_only(tmp_path):
    seen = {}

    def spy(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    orig = gitops.subprocess.run
    gitops.subprocess.run = spy
    try:
        gitops.push_branch(tmp_path, "claude/cpm-task-1-x", "origin", TOKEN, ["main"])
    finally:
        gitops.subprocess.run = orig
    assert TOKEN not in " ".join(seen["cmd"])
    assert "Authorization: Basic" in seen["env"]["GIT_CONFIG_VALUE_0"]
    assert seen["env"]["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert "credential.helper=" in seen["cmd"]


def test_token_never_lands_in_the_database_logs_or_pr_body(ienv, caplog, tmp_path):
    caplog.set_level("DEBUG")
    tid = ienv.completed()
    ienv.integ.integrate(tid, "needs the PC")
    assert TOKEN not in caplog.text
    ienv.engine.dispose()
    db = sqlite3.connect(ienv.settings.db_path)
    dump = "\n".join(db.iterdump())
    db.close()
    assert TOKEN not in dump
    assert TOKEN not in ienv.github.calls[0]["body"]
    for p in ienv.settings.data_dir.rglob("*"):
        if p.is_file() and p.stat().st_size < 5_000_000:
            assert TOKEN.encode() not in p.read_bytes(), p


def test_git_errors_are_redacted(tmp_path):
    from cpm.security.redaction import register_secret

    register_secret(TOKEN)
    (tmp_path / "x").mkdir()
    with pytest.raises(gitops.GitOpError) as e:
        gitops.run(tmp_path / "x", "push", f"https://x:{TOKEN}@127.0.0.1:1/none.git", "main")
    assert TOKEN not in str(e.value)


@pytest.mark.parametrize("ref", ["-x", "--upload-pack=evil", ""])
def test_option_like_refs_are_refused(tmp_path, ref):
    with pytest.raises(gitops.GitOpError):
        gitops.rev_parse(tmp_path, ref) if ref else gitops.resolve_base(tmp_path, "-x")


def test_snapshot_needs_a_full_sha(tmp_path):
    with pytest.raises(gitops.GitOpError):
        gitops.snapshot(tmp_path, "main; rm -rf /")
    with pytest.raises(gitops.GitOpError):
        gitops.merge_conflicts(tmp_path, "HEAD", "HEAD")


def test_diff_path_must_be_a_changed_file_and_is_taken_literally(ienv):
    tid = ienv.testing_task()
    for bad in (":(glob)*", "../README.md", "README.md", "mock_output_task*", "-p"):
        with pytest.raises(Exception, match="not part of the change|cannot"):
            ienv.gate.diff(tid, bad)


def test_hostile_repo_config_cannot_run_a_program_during_review(ienv, tmp_path):
    """fsmonitor / hooks / content filters from the repo's own config must not execute (T-04)."""
    marker = tmp_path / "pwned"
    wt = ienv.sup.worktrees
    tid = ienv.testing_task()
    path = wt.path_for("demo", tid)
    cmd = f"touch {marker}"
    subprocess.run(["git", "-C", str(ienv.repo), "config", "core.fsmonitor", cmd], check=True)  # noqa: S603, S607
    subprocess.run(["git", "-C", str(ienv.repo), "config", "filter.x.clean", cmd], check=True)  # noqa: S603, S607
    (path / ".gitattributes").write_text("* filter=x\n")
    ienv.gate.changes(tid)
    ienv.gate.diff(tid)
    ienv.gate.run(tid)
    assert not marker.exists()
