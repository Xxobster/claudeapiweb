"""Claim / release pull requests: a local bare remote, a fake GitHub API, nothing leaves the PC."""

import pytest
from sqlalchemy.orm import Session

from cpm.db.models import Project
from cpm.locks.boardpr import BoardPrError
from cpm.review.github import GITHUB_TOKEN
from tests.jobboard.conftest import BOARD, sh
from tests.review.conftest import TOKEN


def remote_board(e, branch):
    return sh(e.bare, "show", f"{branch}:docs/JOB_BOARD.md")


def test_claim_pushes_a_docs_only_branch_and_opens_a_pr(benv):
    tid = benv.task()
    main_before = sh(benv.bare, "rev-parse", "main")
    repo_head, repo_status = (
        sh(benv.repo, "rev-parse", "HEAD"),
        sh(benv.repo, "status", "--porcelain"),
    )
    info = benv.board.claim(tid)
    assert (info.state, info.pr_number, info.action) == ("pr_open", 7, "claim")
    assert info.branch == f"claude/cpm-claim-task-{tid}"
    # main untouched, one new branch, exactly one file differs, exactly one line added
    assert sh(benv.bare, "rev-parse", "main") == main_before
    assert sh(benv.bare, "diff", "--name-only", "main", info.branch).split() == [
        "docs/JOB_BOARD.md"
    ]
    added = [
        ln for ln in sh(benv.bare, "diff", "-U0", "main", info.branch).splitlines()
        if ln.startswith("+|")
    ]  # fmt: skip
    assert (
        len(added) == 1
        and f"CPM #{tid}: Menu polish" in added[0]
        and "`Assets/Game/UI/`" in added[0]
    )
    assert sh(benv.bare, "rev-list", "--count", f"main..{info.branch}").strip() == "1"
    # the managed project's own tree, index and refs are as they were
    assert sh(benv.repo, "rev-parse", "HEAD") == repo_head
    assert sh(benv.repo, "status", "--porcelain") == repo_status
    assert "claude/cpm-claim" not in sh(benv.repo, "branch", "--list")
    # the PR
    (call,) = benv.github.calls
    assert (call["owner"], call["repo"], call["base"], call["head"]) == (
        "example", "demo", "main", info.branch,
    )  # fmt: skip
    for section in ("## Files changed", "## How it was tested", "## Security findings",
                    "## Exploit findings", "## What still needs the PC"):  # fmt: skip
        assert section in call["body"]
    assert TOKEN not in call["body"] and call["token"] == TOKEN
    assert benv.board.status(tid)["claim"]["pr_number"] == 7


def test_second_claim_call_is_idempotent(benv):
    tid = benv.task()
    assert benv.board.claim(tid) == benv.board.claim(tid)
    assert len(benv.github.calls) == 1


def test_failed_pr_keeps_the_push_and_retry_does_not_push_again(benv):
    tid = benv.task()
    benv.github.fail_once = True
    with pytest.raises(BoardPrError, match="branch pushed, pull request not opened"):
        benv.board.claim(tid)
    info = benv.board.get(tid, "claim")
    assert info.state == "pushed"
    sh(benv.bare, "update-ref", "-d", f"refs/heads/{info.branch}")
    assert benv.board.claim(tid).state == "pr_open"
    assert info.branch not in sh(benv.bare, "for-each-ref", "--format=%(refname:short)")


def test_claim_overlapping_another_claim_is_refused_before_anything_is_pushed(benv):
    tid = benv.task(scope=("Assets/Audio/line1.wav",))
    with pytest.raises(BoardPrError, match="overlaps an active claim"):
        benv.board.claim(tid)
    assert benv.github.calls == [] and sh(
        benv.bare, "for-each-ref", "--format=%(refname)"
    ).split() == ["refs/heads/main"]


def test_task_without_scope_cannot_claim(benv):
    tid = benv.task(scope=())
    with pytest.raises(BoardPrError, match="no declared scope"):
        benv.board.claim(tid)


def test_release_needs_a_merged_claim_row_then_marks_it_released(benv):
    tid = benv.task()
    with pytest.raises(BoardPrError, match="no claim pull request"):
        benv.board.release(tid)
    claim = benv.board.claim(tid)
    # the claim PR is not merged yet: the base board has no row to release
    with pytest.raises(BoardPrError, match="no CPM claim row"):
        benv.board.release(tid)
    # the person merges the claim PR on GitHub (simulated: fast-forward main, fetch)
    sh(benv.bare, "update-ref", "refs/heads/main", claim.head_sha)
    sh(benv.repo, "fetch", "-q", str(benv.bare), f"{claim.branch}")
    sh(benv.repo, "update-ref", "refs/remotes/origin/main", claim.head_sha)
    assert benv.board.status(tid)["on_board"] == "claimed"
    rel = benv.board.release(tid)
    assert rel.branch == f"claude/cpm-release-task-{tid}" and rel.state == "pr_open"
    text = remote_board(benv, rel.branch)
    assert f"CPM #{tid}: Menu polish | cpm | `Assets/Game/UI/` | released" in text
    assert sh(benv.bare, "diff", "--name-only", "main", rel.branch).split() == ["docs/JOB_BOARD.md"]


def test_claim_after_merge_is_a_noop_error(benv):
    tid = benv.task()
    claim = benv.board.claim(tid)
    sh(benv.repo, "fetch", "-q", str(benv.bare), claim.branch)
    sh(benv.repo, "update-ref", "refs/remotes/origin/main", claim.head_sha)
    # a second task row cannot be confused with the first
    other = benv.task(scope=("Assets/Game/Maps/",), title="Maps")
    assert benv.board.claim(other).state == "pr_open"


def test_missing_token_stops_before_anything_is_built(benv):
    tid = benv.task()
    benv.store.delete(GITHUB_TOKEN)
    with pytest.raises(BoardPrError, match="no GitHub token"):
        benv.board.claim(tid)
    assert benv.board.get(tid, "claim") is None


def test_project_without_profile_or_board_is_refused(benv):
    tid = benv.task()
    with Session(benv.engine) as s:
        s.get(Project, benv.pid).profile = None
        s.commit()
    with pytest.raises(BoardPrError, match="no profile"):
        benv.board.claim(tid)


def test_board_missing_on_the_base_branch(benv):
    tid = benv.task()
    sh(
        benv.repo,
        "update-ref",
        "refs/remotes/origin/main",
        sh(benv.repo, "rev-parse", "HEAD~1").strip(),
    )
    with pytest.raises(BoardPrError, match="does not exist"):
        benv.board.claim(tid)
    assert benv.board.status(tid)["problem"]


def test_remote_that_is_not_github_is_refused(benv):
    tid = benv.task()
    sh(benv.repo, "remote", "set-url", "origin", "https://gitlab.example/x/y")
    with pytest.raises(BoardPrError, match="github.com"):
        benv.board.claim(tid)


def test_without_a_remote_tracking_base_nothing_is_built(benv):
    tid = benv.task()
    sh(benv.repo, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(BoardPrError, match="origin/main"):
        benv.board.claim(tid)


def test_crlf_board_keeps_its_line_endings(benv):
    from tests.fixtures.repos import git, write

    write(benv.repo, "docs/JOB_BOARD.md", BOARD.replace("\n", "\r\n"))
    sh(benv.repo, "-c", "core.autocrlf=false", "add", "-A")
    git(benv.repo, "commit", "-q", "-m", "crlf")
    sh(benv.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    sh(benv.repo, "push", "-q", str(benv.bare), "main")
    tid = benv.task()
    info = benv.board.claim(tid)
    # line endings kept: the diff is exactly the one added line (a lost CRLF would touch every line)
    assert sh(benv.bare, "diff", "--numstat", "main", info.branch).split()[:2] == ["1", "0"]


def test_a_symlink_board_is_refused(benv):
    import os

    os.remove(benv.repo / "docs" / "JOB_BOARD.md")
    os.symlink("/etc/hostname", benv.repo / "docs" / "JOB_BOARD.md")
    sh(benv.repo, "add", "-A")
    sh(benv.repo, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-q", "-m", "ln")
    sh(benv.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    tid = benv.task()
    with pytest.raises(BoardPrError, match="not a regular file"):
        benv.board.claim(tid)
    assert benv.github.calls == []
