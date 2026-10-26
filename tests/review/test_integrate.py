"""pr_only integration: scenario 13 (unsafe git integration is rejected) and 16 (unrelated local
changes preserved). The remote is a local bare repository reached through `url.insteadOf`, the
GitHub API is a fake: nothing leaves the machine."""

import pytest

from cpm.review import gitops
from cpm.review.github import GITHUB_TOKEN
from cpm.review.integrate import IntegrationError, pr_body
from tests.fixtures.repos import write
from tests.review.conftest import TOKEN, sh


def test_full_flow_commits_pushes_and_opens_a_pull_request(ienv):
    tid = ienv.completed()
    main_before = sh(ienv.bare, "rev-parse", "main")
    info = ienv.integ.integrate(tid, "Check on the PC: nothing")
    assert (info.state, info.pr_number) == ("pr_open", 7)
    assert info.pr_url == "https://github.com/example/demo/pull/7"
    assert ienv.tasks.get(tid).status == "INTEGRATED"
    # the remote got the task branch, main is untouched
    branches = sh(ienv.bare, "for-each-ref", "--format=%(refname:short)").split()
    assert branches == [info.branch, "main"] or sorted(branches) == sorted([info.branch, "main"])
    assert sh(ienv.bare, "rev-parse", "main") == main_before
    assert sh(ienv.bare, "rev-parse", info.branch).strip() == info.head_sha
    # exactly one commit on top of main, holding exactly the worker's file
    assert sh(ienv.bare, "diff", "--name-only", "main", info.branch).split() == [
        f"mock_output_task{tid}.txt"
    ]
    # PR call
    (call,) = ienv.github.calls
    assert (call["owner"], call["repo"], call["base"], call["head"]) == (
        "example",
        "demo",
        "main",
        info.branch,
    )
    assert call["token"] == TOKEN and info.branch.startswith("claude/cpm-task-")
    for section in ("## Files changed", "## How it was tested", "## Security findings",
                    "## Exploit findings", "## What still needs the PC"):  # fmt: skip
        assert section in call["body"]
    assert (
        "Check on the PC: nothing" in call["body"] and f"mock_output_task{tid}.txt" in call["body"]
    )
    assert TOKEN not in call["body"]


def test_second_call_is_idempotent(ienv):
    tid = ienv.completed()
    a = ienv.integ.integrate(tid)
    b = ienv.integ.integrate(tid)
    assert a == b and len(ienv.github.calls) == 1


def test_failed_pull_request_keeps_the_push_and_a_retry_does_not_push_again(ienv):
    tid = ienv.completed()
    ienv.github.fail_once = True
    with pytest.raises(IntegrationError, match="branch pushed, pull request not opened"):
        ienv.integ.integrate(tid)
    assert ienv.integ.get(tid).state == "pushed"
    assert ienv.tasks.get(tid).status == "COMPLETED"
    # remove the remote branch: a retry must NOT push again (it would show up again)
    branch = ienv.integ.get(tid).branch
    sh(ienv.bare, "update-ref", "-d", f"refs/heads/{branch}")
    assert ienv.integ.integrate(tid).state == "pr_open"
    assert branch not in sh(ienv.bare, "for-each-ref", "--format=%(refname:short)")


def test_only_completed_tasks_with_a_passed_gate_are_integrated(ienv):
    tid = ienv.testing_task()
    with pytest.raises(IntegrationError, match="COMPLETED"):
        ienv.integ.integrate(tid)
    assert ienv.github.calls == []


def test_missing_token_stops_before_anything_is_committed_or_pushed(ienv):
    tid = ienv.completed()
    ienv.store.delete(GITHUB_TOKEN)
    with pytest.raises(IntegrationError, match="no GitHub token"):
        ienv.integ.integrate(tid)
    wt = ienv.sup.worktrees.path_for("demo", tid)
    assert sh(wt, "status", "--porcelain").strip()  # still uncommitted: nothing happened
    assert ienv.integ.get(tid) is None


def test_remote_that_is_not_github_is_refused(ienv):
    tid = ienv.completed()
    sh(ienv.repo, "remote", "set-url", "origin", "https://gitlab.example/x/y")
    with pytest.raises(IntegrationError, match="github.com"):
        ienv.integ.integrate(tid)


def test_work_changed_after_the_gate_is_not_committed(ienv):
    tid = ienv.completed()
    write(ienv.sup.worktrees.path_for("demo", tid), "sneaky.txt", "added after the gate\n")
    with pytest.raises(IntegrationError, match="changed after the acceptance gate"):
        ienv.integ.integrate(tid)
    assert ienv.integ.get(tid) is None and ienv.github.calls == []
    assert sh(ienv.bare, "for-each-ref", "--format=%(refname:short)").split() == ["main"]


def test_base_that_moved_into_a_conflict_blocks_the_push(ienv):
    tid = ienv.completed()
    name = f"mock_output_task{tid}.txt"
    write(ienv.repo, name, "a conflicting change on main\n")
    sh(ienv.repo, "add", "-A")
    sh(
        ienv.repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@e.invalid",
        "commit",
        "-q",
        "-m",
        "clash",
    )
    with pytest.raises(IntegrationError, match="no longer merges cleanly"):
        ienv.integ.integrate(tid)
    assert ienv.github.calls == []
    assert sh(ienv.bare, "for-each-ref", "--format=%(refname:short)").split() == ["main"]


def test_unrelated_local_changes_in_the_main_tree_survive_the_whole_flow(ienv):
    write(ienv.repo, "README.md", "# demo\nunsaved work in progress\n")
    write(ienv.repo, "notes/untracked.txt", "keep me\n")
    sh(ienv.repo, "stash", "list")
    head, status = sh(ienv.repo, "rev-parse", "HEAD"), sh(ienv.repo, "status", "--porcelain")
    tid = ienv.completed()
    ienv.integ.integrate(tid)
    assert sh(ienv.repo, "rev-parse", "HEAD") == head
    assert sh(ienv.repo, "status", "--porcelain") == status
    assert sh(ienv.repo, "branch", "--show-current").strip() == "main"
    assert (ienv.repo / "notes/untracked.txt").read_text() == "keep me\n"
    assert sh(ienv.repo, "stash", "list") == ""


def test_worker_that_committed_itself_is_integrated_without_a_second_commit(ienv):
    tid = ienv.testing_task()
    wt = ienv.sup.worktrees.path_for("demo", tid)
    sh(wt, "add", "-A")
    sh(
        wt,
        "-c",
        "user.name=w",
        "-c",
        "user.email=w@e.invalid",
        "commit",
        "-q",
        "-m",
        "worker commit",
    )
    assert ienv.gate.run(tid).task_status == "COMPLETED"
    info = ienv.integ.integrate(tid)
    assert (
        sh(ienv.bare, "log", "--format=%s", "main.." + info.branch).split("\n")[0]
        == "worker commit"
    )


# ------------------------------------------------------------------ push guard (scenario 13)
@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "master",
        "claude/main",
        "-x",
        "../x",
        "feature/x",
        "claude/../main",
        "claude/x.lock",
        "claude/",
        "claude/a b",
        "",
        "refs/heads/main",
        "claude/x\n",
    ],
)
def test_guard_push_refuses_unsafe_branches(branch):
    with pytest.raises(gitops.GitOpError):
        gitops.guard_push(branch, ["main", "master"])


def test_guard_push_accepts_task_branches():
    gitops.guard_push("claude/cpm-task-3-add-the-thing", ["main"])


def test_push_branch_never_forces_and_rejects_a_diverged_remote(ienv):
    tid = ienv.completed()
    info = ienv.integ.integrate(tid)
    # someone else pushed a different commit to the same branch name: our push must fail, not force
    other = ienv.repo.parent / "other"
    sh(ienv.repo.parent, "clone", "-q", str(ienv.bare), str(other))
    sh(other, "checkout", "-q", "-b", "tmp", "main")
    write(other, "x.txt", "x\n")
    sh(other, "add", "-A")
    sh(other, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "x")
    sh(other, "push", "-q", "--force", "origin", f"tmp:{info.branch}")
    remote_head = sh(ienv.bare, "rev-parse", info.branch)
    with pytest.raises(gitops.GitOpError):
        gitops.push_branch(ienv.repo, info.branch, "origin", None, ["main"])
    assert sh(ienv.bare, "rev-parse", info.branch) == remote_head


def test_pr_body_always_has_the_required_sections():
    body = pr_body("T", {"files": [], "review": {}, "tests": {}}, "")
    for h in ("Files changed", "Security findings", "Exploit findings", "What still needs the PC"):
        assert f"## {h}" in body
    assert "Nothing recorded by CPM" in body
