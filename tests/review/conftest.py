"""Fixtures for the gate and integration tests: a trusted fixture project that has tests."""

from __future__ import annotations

import subprocess  # noqa: S404

import pytest
from sqlalchemy.orm import Session

from cpm.db.models import Project
from cpm.review.gate import GateService
from cpm.review.github import GITHUB_TOKEN
from cpm.review.integrate import IntegrationService
from cpm.review.reviewer import MockReviewer, ReviewerSource
from cpm.review.testrun import TestOutcome
from tests.fixtures.repos import git, write
from tests.workers.conftest import env as worker_env  # noqa: F401  (fixture reused)


class FakeRunner:
    """Scripted test runner: pops outcomes (exit code or text) and records the calls."""

    __test__ = False

    def __init__(self, *outcomes: int | tuple[int, str]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[list[str], str]] = []

    def run(self, argv, cwd, timeout_s):
        self.calls.append((list(argv), str(cwd)))
        item = self.outcomes.pop(0) if self.outcomes else 0
        code, text = item if isinstance(item, tuple) else (item, "ok" if item == 0 else "FAILED x")
        return TestOutcome(list(argv), code, False, 0.1, text)


class GitHubFake:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_once = False

    def create_pull_request(self, owner, repo, *, head, base, title, body, token):
        from cpm.review.github import GitHubError, PullRequest

        self.calls.append(
            {"owner": owner, "repo": repo, "head": head, "base": base, "title": title,
             "body": body, "token": token}
        )  # fmt: skip
        if self.fail_once:
            self.fail_once = False
            raise GitHubError("GitHub answered 502 - unexpected answer")
        return PullRequest(7, f"https://github.com/{owner}/{repo}/pull/7")


class NoReviewer(ReviewerSource):
    def for_billing(self, billing_mode):
        from cpm.review.reviewer import ReviewerUnavailable

        raise ReviewerUnavailable("no live reviewer is configured")


@pytest.fixture
def renv(worker_env, tmp_path):  # noqa: F811
    e = worker_env
    # the managed project has a test suite (so a test command is detected) and is trusted
    write(e.repo, "pyproject.toml", "[tool.pytest.ini_options]\n")
    write(e.repo, "tests/test_a.py", "def test_a():\n    pass\n")
    git(e.repo, "add", "-A")
    git(e.repo, "commit", "-q", "-m", "tests")
    with Session(e.engine) as s:
        p = s.get(Project, e.pid)
        p.workspace_trusted = True
        s.commit()
    e.runner = FakeRunner()
    e.reviewer = MockReviewer()
    e.gate = GateService(
        e.engine, e.sup, e.sup.worktrees, ReviewerSource(mock=e.reviewer), e.runner
    )
    e.github = GitHubFake()
    e.integ = IntegrationService(e.engine, e.sup.worktrees, e.store, e.github)

    def testing_task(title="Add the output", scope=(), criteria=("It exists",), **kw):
        """Create a READY mock task, run the worker, return the task id in TESTING."""
        t = e.new_task(title, declared_scope=list(scope), acceptance=list(criteria), **kw)
        run = e.sup.start(t.id, heartbeat_s=0.2)
        e.sup.wait(run.id, 30)
        assert e.tasks.get(t.id).status == "TESTING"
        return t.id

    def rerun(task_id):
        """Wait for a correction run to end (the task returns to TESTING)."""
        for r in e.sup.list_runs(task_id):
            e.sup.wait(r.id, 30)

    e.testing_task, e.rerun = testing_task, rerun
    e.sh = lambda *a: (
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "git",
                "-C",
                str(e.repo),
                *a,
            ],
            capture_output=True,
            text=True,
            check=True,  # noqa: S607
        ).stdout
    )
    return e


TOKEN = "ghp_" + "a1B2c3D4e5" * 3


def sh(root, *a):
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *a],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,  # noqa: S607
    ).stdout


@pytest.fixture
def ienv(renv, tmp_path):
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    # the fetch URL stays github.com (so the PR target parses); pushes go to the local bare repo
    sh(renv.repo, "config", "remote.origin.pushurl", str(bare))
    sh(renv.repo, "push", "-q", str(bare), "main")
    renv.bare = bare
    renv.store.set(GITHUB_TOKEN, TOKEN)
    renv.completed = lambda **kw: _complete(renv, **kw)
    return renv


def _complete(e, **kw):
    tid = e.testing_task(**kw)
    assert e.gate.run(tid).task_status == "COMPLETED"
    return tid


def _complete(e, **kw):
    tid = e.testing_task(**kw)
    assert e.gate.run(tid).task_status == "COMPLETED"
    return tid
