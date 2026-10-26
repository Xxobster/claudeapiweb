"""Git worktrees for workers (PLAN.md WP5; docs/THREATS.md T-03, T-05, T-22).

A worker only ever edits inside its own worktree `<worktree_root>/<project slug>/task-<id>` on its
own branch `claude/cpm-task-<id>-<slug>`; the managed project's main tree is never written.
Every git call uses an argument list and the hardened command line of `discovery.gitinfo`
(no fsmonitor, no hooks, repo content filters blanked, no prompts, no network command).
Worktrees that hold changes or commits are never deleted.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from cpm.discovery.gitinfo import GitUnavailableError, _filter_overrides, _git
from cpm.security.paths import PathPolicyError, resolve_within, validate_root

GIT_TIMEOUT_S = 300  # a checkout of a large game repository can take minutes
_SLUG = re.compile(r"[^a-z0-9]+")
_SAFE_PART = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class WorktreeError(RuntimeError):
    """The worktree cannot be created, verified or removed. The message never has file contents."""


class WorktreeHasChanges(WorktreeError):
    """Refusing to delete a worktree that holds uncommitted changes or unmerged commits."""


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    branch: str
    created: bool
    base: str


def branch_name(task_id: int, title: str) -> str:
    slug = _SLUG.sub("-", title.lower()).strip("-")[:40].strip("-")
    return f"claude/cpm-task-{task_id}" + (f"-{slug}" if slug else "")


def _key(p: Path | str) -> str:
    return os.path.normcase(os.path.realpath(p))


class WorktreeManager:
    def __init__(self, root: Path) -> None:
        self.root = root

    # ------------------------------------------------------------------ paths
    def _ensure_root(self, project: Path) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        real = validate_root(self.root)
        proj = Path(os.path.realpath(project))
        if real == proj or proj in real.parents or real in proj.parents:
            raise WorktreeError("worktree root must not overlap the managed project")
        return real

    def path_for(self, project_slug: str, task_id: int) -> Path:
        if not _SAFE_PART.match(project_slug):
            raise WorktreeError("unsafe project slug")
        return self.root / project_slug / f"task-{int(task_id)}"

    def check(self, path: Path | str) -> Path:
        """Re-validate a stored worktree path right before it is used (TOCTOU, THREATS WP2)."""
        try:
            return resolve_within(self.root, path, must_exist=True)
        except PathPolicyError as exc:
            raise WorktreeError(f"worktree path refused: {exc}") from exc

    # ------------------------------------------------------------------ git helpers
    @staticmethod
    def _git(root: Path, *args: str, config: tuple[str, ...] = ()) -> str:
        try:
            return _git(root, *args, config=config, timeout=GIT_TIMEOUT_S)
        except GitUnavailableError as exc:
            raise WorktreeError(f"git failed: {exc}") from exc

    def _ok(self, root: Path, *args: str) -> bool:
        try:
            self._git(root, *args)
        except WorktreeError:
            return False
        return True

    def _base_ref(self, project: Path) -> str:
        for ref in ("refs/remotes/origin/main", "refs/remotes/origin/master"):
            if self._ok(project, "rev-parse", "--verify", "-q", ref):
                return ref.removeprefix("refs/remotes/")
        return "HEAD"

    def _registered(self, project: Path) -> set[str]:
        out = self._git(project, "worktree", "list", "--porcelain", "-z")
        return {_key(e[9:]) for e in out.split("\0") if e.startswith("worktree ")}

    # ------------------------------------------------------------------ API
    def ensure(
        self, project: Path, project_slug: str, task_id: int, title: str, base: str | None = None
    ) -> WorktreeInfo:
        """Create the task's worktree, or reuse it (a retry continues the previous work)."""
        if not (project / ".git").exists():
            raise WorktreeError("project is not a git repository")
        self._ensure_root(project)
        path = self.path_for(project_slug, task_id)
        branch = branch_name(task_id, title)
        if not self._ok(project, "check-ref-format", "--branch", branch):
            raise WorktreeError("derived branch name is not valid")
        if path.exists():
            if _key(path) not in self._registered(project):
                raise WorktreeError("target path exists but is not a worktree of this project")
            self.verify(project, path)
            return WorktreeInfo(Path(_key(path)), branch, False, "")
        path.parent.mkdir(parents=True, exist_ok=True)
        base_ref = base or self._base_ref(project)
        cfg = _filter_overrides(project)
        if self._ok(project, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
            args = ["worktree", "add", "--", str(path), branch]  # branch survives a removed tree
        else:
            args = ["worktree", "add", "--no-track", "-b", branch, "--", str(path), base_ref]
        self._git(project, *args, config=cfg)
        self.verify(project, path)
        return WorktreeInfo(Path(_key(path)), branch, True, base_ref)

    def verify(self, project: Path, path: Path) -> None:
        """The path must be a linked worktree of `project`, never the project's own tree."""
        real = self.check(path)
        if _key(real) == _key(project):
            raise WorktreeError("worktree is the managed project's main tree")
        common_wt = self._git(real, "rev-parse", "--path-format=absolute", "--git-common-dir")
        common_pr = self._git(project, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if _key(common_wt.strip()) != _key(common_pr.strip()):
            raise WorktreeError("worktree does not belong to the managed project")
        top = self._git(real, "rev-parse", "--show-toplevel")
        if _key(top.strip()) != _key(real):
            raise WorktreeError("worktree path is not a repository top level")

    def status(self, path: Path) -> tuple[int, int]:
        """(uncommitted entries, commits that exist only on this worktree's branch)."""
        real = self.check(path)
        entries = self._git(
            real, "status", "--porcelain=v1", "-z", "--untracked-files=all",
            config=_filter_overrides(real),
        )  # fmt: skip
        dirty = len([e for e in entries.split("\0") if e])
        branch = self._git(real, "symbolic-ref", "--short", "-q", "HEAD").strip()
        ahead = self._git(
            real, "rev-list", "--count", branch,
            # --exclude patterns are relative to refs/heads
            "--not", f"--exclude={branch}", "--branches", "--remotes",
        )  # fmt: skip
        return dirty, int(ahead.strip() or 0)

    def remove(self, project: Path, project_slug: str, task_id: int) -> bool:
        """Delete a clean worktree and its branch; refuses (WorktreeHasChanges) to lose work."""
        path = self.path_for(project_slug, task_id)
        if not path.exists():
            return False
        self.verify(project, path)
        dirty, ahead = self.status(path)
        if dirty or ahead:
            raise WorktreeHasChanges(
                f"worktree has {dirty} uncommitted entries and {ahead} unmerged commits; kept"
            )
        branch = self._git(path, "symbolic-ref", "--short", "-q", "HEAD").strip()
        self._git(project, "worktree", "remove", "--", str(path))  # no --force
        self._ok(project, "branch", "-d", "--", branch)
        return True
