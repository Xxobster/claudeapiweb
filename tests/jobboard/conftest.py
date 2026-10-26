"""A fixture project with a job board on origin/main; pushes go to a local bare repo."""

import subprocess  # noqa: S404

import pytest
from sqlalchemy.orm import Session

from cpm.db.models import Project
from cpm.locks.boardpr import JobBoardService
from cpm.review.github import GITHUB_TOKEN
from tests.fixtures.repos import git, write
from tests.review.conftest import TOKEN, GitHubFake
from tests.workers.conftest import env as worker_env  # noqa: F401

BOARD = """# Job board

| Job | Owner | Files | Status | Since |
|---|---|---|---|---|
| Voice pass | desktop | `Assets/Audio/` | active | 2026-10-01 |
"""


def sh(root, *a):
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *a],  # noqa: S607
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip


@pytest.fixture
def benv(worker_env, tmp_path):  # noqa: F811
    e = worker_env
    write(e.repo, "docs/JOB_BOARD.md", BOARD)
    git(e.repo, "add", "-A")
    git(e.repo, "commit", "-q", "-m", "board")
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    sh(e.repo, "config", "remote.origin.pushurl", str(bare))  # fetch URL stays github.com
    sh(e.repo, "push", "-q", str(bare), "main")
    sh(e.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    with Session(e.engine) as s:
        p = s.get(Project, e.pid)
        p.profile = "nirandyr"
        s.commit()
    e.store.set(GITHUB_TOKEN, TOKEN)
    e.bare, e.github = bare, GitHubFake()
    e.board = JobBoardService(e.engine, e.sup.worktrees, e.store, e.github)
    e.task = lambda scope=("Assets/Game/UI/",), title="Menu polish": (
        e.new_task(title, declared_scope=list(scope)).id
    )
    return e
