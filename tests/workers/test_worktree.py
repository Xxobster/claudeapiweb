import pytest

from cpm.workers.worktree import (
    WorktreeError,
    WorktreeHasChanges,
    WorktreeManager,
    branch_name,
)
from tests.fixtures.repos import git, make_repo, write


@pytest.fixture
def wm(tmp_path):
    repo = make_repo(tmp_path / "game repo é")
    return WorktreeManager(tmp_path / "wt root"), repo


def test_branch_name_is_safe():
    assert (
        branch_name(7, "Fix: the `rm -rf /` bug; $(x)") == "claude/cpm-task-7-fix-the-rm-rf-bug-x"
    )
    assert branch_name(3, "§§§") == "claude/cpm-task-3"


def test_create_reuse_and_isolation(wm):
    mgr, repo = wm
    info = mgr.ensure(repo, "demo", 1, "First task")
    assert info.created and info.path.is_dir() and info.branch == "claude/cpm-task-1-first-task"
    assert info.path != repo and mgr.root in info.path.parents
    write(info.path, "new.txt")
    assert not (repo / "new.txt").exists()  # the main tree is never touched
    again = mgr.ensure(repo, "demo", 1, "First task")
    assert not again.created and again.path == info.path
    assert (info.path / "new.txt").exists()  # a retry keeps the previous work


def test_non_repo_and_overlapping_root_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError, match="not a git repository"):
        WorktreeManager(tmp_path / "w").ensure(plain, "demo", 1, "t")
    repo = make_repo(tmp_path / "r")
    with pytest.raises(WorktreeError, match="overlap"):
        WorktreeManager(repo / "inside").ensure(repo, "demo", 1, "t")


def test_unsafe_slug_and_foreign_path_refused(wm, tmp_path):
    mgr, repo = wm
    with pytest.raises(WorktreeError):
        mgr.path_for("../evil", 1)
    # a plain directory squatting on the target path is never adopted or deleted
    target = mgr.path_for("demo", 5)
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("mine")
    with pytest.raises(WorktreeError, match="not a worktree"):
        mgr.ensure(repo, "demo", 5, "t")
    assert (target / "keep.txt").exists()


def test_check_rejects_paths_outside_root(wm, tmp_path):
    mgr, repo = wm
    mgr.ensure(repo, "demo", 1, "t")
    for bad in (repo, tmp_path, mgr.root / ".." / "game repo é"):
        with pytest.raises(WorktreeError):
            mgr.check(bad)


def test_verify_rejects_the_main_tree_and_foreign_worktrees(wm, tmp_path):
    mgr, repo = wm
    mgr.root.mkdir(parents=True, exist_ok=True)
    other = make_repo(tmp_path / "other")
    foreign = mgr.root / "other-task"
    git(other, "worktree", "add", "-q", "-b", "x", str(foreign))
    with pytest.raises(WorktreeError, match="does not belong"):
        mgr.verify(repo, foreign)


def test_remove_refuses_dirty_or_committed_work_and_removes_clean(wm):
    mgr, repo = wm
    info = mgr.ensure(repo, "demo", 1, "t")
    write(info.path, "wip.txt")
    with pytest.raises(WorktreeHasChanges):
        mgr.remove(repo, "demo", 1)
    git(info.path, "add", "-A")
    git(info.path, "commit", "-q", "-m", "work")
    with pytest.raises(WorktreeHasChanges, match="unmerged commits"):
        mgr.remove(repo, "demo", 1)
    assert (info.path / "wip.txt").exists()
    # merge the work into the base branch: the worktree is now clean and fully merged
    git(repo, "merge", "-q", "--ff-only", info.branch)
    assert mgr.remove(repo, "demo", 1) is True
    assert not info.path.exists()
    assert mgr.remove(repo, "demo", 1) is False


def test_hostile_repo_config_does_not_run_programs(wm, tmp_path):
    mgr, repo = wm
    marker = tmp_path / "pwned"
    prog = tmp_path / "evil.sh"
    prog.write_text(f"#!/bin/sh\ntouch {marker}\n")
    prog.chmod(0o755)
    write(repo, ".gitattributes", "*.dat filter=x\n")
    write(repo, "a.dat")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attrs")
    # the repository config turns hostile only after the fixture commit
    git(repo, "config", "core.fsmonitor", str(prog))
    git(repo, "config", "filter.x.clean", str(prog))
    git(repo, "config", "filter.x.smudge", str(prog))
    info = mgr.ensure(repo, "demo", 1, "t")
    write(info.path, "b.dat", "changed")
    mgr.status(info.path)
    assert not marker.exists()


def test_hooks_do_not_run_on_worktree_creation(wm, tmp_path):
    mgr, repo = wm
    marker = tmp_path / "hook-ran"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    mgr.ensure(repo, "demo", 1, "t")
    assert not marker.exists()
