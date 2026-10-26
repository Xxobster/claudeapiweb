"""Docs-only pull requests that claim and release rows on the project's job board (WP11).

Nirandyr rule 40: a job is claimed on `docs/JOB_BOARD.md` through a docs-only PR. CPM builds that
PR with git plumbing on top of the base commit (`commit_blob`): nothing is checked out, the
managed project's main tree and index are never touched, only `docs/JOB_BOARD.md` differs, and
only a `claude/cpm-claim-*` / `claude/cpm-release-*` branch is pushed (never forced, never main).
The PR is opened and never merged: the person merges it on GitHub. Both actions are explicit
clicks (`POST /api/tasks/{id}/job-board/claim|release`); nothing claims on its own.

Steps are persisted (`job_board_claims`), so a retry resumes after the push and never repeats it.
The base is the local `origin/<default>` as last fetched; a stale base shows up as a merge
conflict on GitHub, never as a silent overwrite.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from cpm.db.models import Event, JobBoardClaim, Project, utcnow
from cpm.db.settings_repo import get_setting
from cpm.discovery.gitinfo import GitUnavailableError, read_remotes
from cpm.locks import boardwriter
from cpm.locks.adapters.jobboard import RELEASED
from cpm.locks.boardwriter import BoardError
from cpm.profiles import ProfileError, load_profile
from cpm.review import gitops
from cpm.review.github import (
    GITHUB_TOKEN,
    GitHubClient,
    GitHubError,
    HttpGitHubClient,
    parse_remote,
)
from cpm.review.integrate import _protected_branches
from cpm.security.paths import PathPolicyError, resolve_in_any, validate_relative
from cpm.security.redaction import redact
from cpm.security.secrets_store import SecretStore, SecretStoreUnavailable
from cpm.tasks.service import NotFound, TaskError, TaskService
from cpm.tasks.states import TERMINAL
from cpm.workers.worktree import WorktreeManager

log = logging.getLogger("cpm.locks")
MAX_BOARD_BYTES = 1_000_000
ACTIONS = ("claim", "release")


class BoardPrError(TaskError):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ClaimInfo:
    task_id: int
    action: str
    state: str  # committed | pushed | pr_open
    branch: str
    head_sha: str
    pr_number: int | None
    pr_url: str | None
    board_path: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def pr_body(action: str, task: Any, board_path: str, row: str, files: list[str]) -> str:
    """Sections that Nirandyr rules 37-40 require; all four are always present."""
    verb = "Claim" if action == "claim" else "Release"
    lines = [f"{verb} job board row for CPM task #{task.id}: {task.title}", ""]
    lines += ["## Files changed", "", f"- `{board_path}` (one table row {action}ed)", ""]
    if action == "claim":
        lines += ["Claimed paths:", ""] + [f"- `{f}`" for f in files[:50]] + [""]
    lines += ["Row:", "", f"    {row}", ""]
    lines += [
        "## How it was tested",
        "",
        "- Docs-only change built by CPM from the base commit; only the job board file differs.",
        "- No code, no tests to run.",
        "",
        "## Security findings",
        "",
        "- None: no code, no secrets, no paths outside the job board. The row text is sanitised "
        "(no `|`, backtick or control characters) and the paths went through the path policy.",
        "",
        "## Exploit findings",
        "",
        "- None: the diff touches a single file; a person should still read the row.",
        "",
        "## What still needs the PC",
        "",
        "Nothing. Merge this PR on GitHub to make the "
        + ("claim" if action == "claim" else "release")
        + " visible to the other threads.",
        "",
        "_Opened by Claude Project Manager. CPM never merges: review and merge on GitHub._",
    ]
    return "\n".join(lines)


class JobBoardService:
    def __init__(
        self,
        engine: Engine,
        worktrees: WorktreeManager,
        secrets: SecretStore,
        github: GitHubClient | None = None,
    ) -> None:
        self.engine = engine
        self.worktrees = worktrees
        self.secrets = secrets
        self.github: GitHubClient = github or HttpGitHubClient()
        self.tasks = TaskService(engine)

    # ------------------------------------------------------------------ read
    def get(self, task_id: int, action: str) -> ClaimInfo | None:
        with Session(self.engine) as s:
            row = s.scalars(
                select(JobBoardClaim).where(
                    JobBoardClaim.task_id == task_id, JobBoardClaim.action == action
                )
            ).first()
            return _info(row) if row else None

    def status(self, task_id: int) -> dict[str, Any]:
        """What CPM has done for this task, plus whether the board at the base shows the row."""
        self.tasks.get(task_id)
        out: dict[str, Any] = {
            "claim": _d(self.get(task_id, "claim")),
            "release": _d(self.get(task_id, "release")),
            "board_path": None,
            "on_board": None,
            "problem": None,
        }
        try:
            ctx = self._context(task_id)
            out["board_path"] = ctx.board_path
            raw = gitops.read_blob(ctx.ppath, ctx.base_sha, ctx.board_path, MAX_BOARD_BYTES)
            if raw is None:
                out["problem"] = "the job board file does not exist on the base branch"
            else:
                text, _ = boardwriter.decode(raw)
                lines = text.split("\n")
                rows = [
                    ln for ln in lines if boardwriter.marker(task_id) in ln and "cpm" in ln.lower()
                ]
                out["on_board"] = (
                    "none"
                    if not rows
                    else "released"
                    if all(_released(r) for r in rows)
                    else "claimed"
                )
        except (BoardPrError, BoardError, gitops.GitOpError) as exc:
            out["problem"] = redact(str(exc))[:300]
        return out

    # ------------------------------------------------------------------ run
    def claim(self, task_id: int) -> ClaimInfo:
        return self._run(task_id, "claim")

    def release(self, task_id: int) -> ClaimInfo:
        return self._run(task_id, "release")

    def _run(self, task_id: int, action: str) -> ClaimInfo:
        try:
            return self._do(task_id, action)
        except (BoardError, gitops.GitOpError, PathPolicyError, GitUnavailableError) as exc:
            raise BoardPrError(redact(str(exc))[:300]) from exc

    def _do(self, task_id: int, action: str) -> ClaimInfo:
        task = self.tasks.get(task_id)
        existing = self.get(task_id, action)
        if existing and existing.state == "pr_open":
            return existing
        if action == "claim":
            if task.status in TERMINAL:
                raise BoardPrError(f"a {task.status} task cannot claim files")
            if self.get(task_id, "release"):
                raise BoardPrError("this task already released its claim; make a new task")
        elif not (self.get(task_id, "claim")):
            raise BoardPrError("this task has no claim pull request to release")
        ctx = self._context(task_id)
        token = self._token()
        branch = f"claude/cpm-{action}-task-{task_id}"
        gitops.guard_push(branch, ctx.protected)

        if existing is None:
            raw = gitops.read_blob(ctx.ppath, ctx.base_sha, ctx.board_path, MAX_BOARD_BYTES)
            if raw is None:
                raise BoardPrError(f"{ctx.board_path} does not exist on {ctx.base_ref}")
            text, eol = boardwriter.decode(raw)
            if action == "claim":
                edit = boardwriter.add_claim(
                    text,
                    task_id=task_id,
                    title=task.title,
                    files=list(task.declared_scope),
                    today=date.today().isoformat(),
                )
            else:
                edit = boardwriter.release_claim(text, task_id=task_id)
            if not edit.changed:
                raise BoardPrError(
                    f"the board already shows this task as {action}ed; nothing to publish"
                )
            head = gitops.commit_blob(
                ctx.ppath,
                ctx.base_sha,
                ctx.board_path,
                boardwriter.encode(edit.text, eol),
                f"Job board: {action} CPM task #{task_id}",
            )
            self._save(task_id, action, ctx.board_path, branch, head, "committed")
            info = self.get(task_id, action)
            assert info is not None  # noqa: S101
            row = edit.row
        else:
            info, head, row = existing, existing.head_sha, "(see the branch)"
        if info.state == "committed":
            gitops.push_branch(ctx.ppath, branch, "origin", token, ctx.protected, source=head)
            self._save(task_id, action, ctx.board_path, branch, head, "pushed")
        files = list(task.declared_scope)
        try:
            pr = self.github.create_pull_request(
                ctx.owner,
                ctx.repo,
                head=branch,
                base=ctx.base_branch,
                title=f"Job board: {action} CPM task #{task_id}"[:250],
                body=pr_body(action, task, ctx.board_path, row, files),
                token=token,
            )
        except GitHubError as exc:
            raise BoardPrError(f"branch pushed, pull request not opened: {exc}") from exc
        self._save(task_id, action, ctx.board_path, branch, head, "pr_open", pr.number, pr.url)
        out = self.get(task_id, action)
        assert out is not None  # noqa: S101
        return out

    # ------------------------------------------------------------------ helpers
    def _context(self, task_id: int) -> _Ctx:
        task = self.tasks.get(task_id)
        with Session(self.engine) as s:
            project = s.get(Project, task.project_id)
            if project is None:
                raise NotFound(f"task {task_id} has no project")
            s.expunge(project)
        if project.integration_mode != "pr_only":
            raise BoardPrError("only the pr_only integration mode exists")
        if not project.profile:
            raise BoardPrError("this project has no profile, so no job board is configured")
        try:
            profile = load_profile(project.profile)
        except ProfileError as exc:
            raise BoardPrError(str(exc)) from exc
        board = profile.external_locks.get("job_board")
        if not board:
            raise BoardPrError("the project profile does not name a job board file")
        try:
            board_path = validate_relative(board)
        except PathPolicyError as exc:
            raise BoardPrError(f"job board path refused: {exc}") from exc
        ppath = resolve_in_any(
            get_setting(self.engine, "approved_roots", []) or [], Path(project.path)
        )
        target = parse_remote(read_remotes(ppath).get("origin"))
        if target is None:
            raise BoardPrError("pull requests need an 'origin' remote on github.com")
        base_ref = self.worktrees._base_ref(ppath)
        if not base_ref.startswith("origin/"):
            raise BoardPrError("no origin/main branch to build the claim on; fetch the project")
        base_sha = gitops.resolve_base(ppath, base_ref)
        base_branch = gitops.default_branch_name(ppath, base_ref)
        return _Ctx(
            ppath, board_path, base_ref, base_sha, base_branch, target[0], target[1],
            _protected_branches(project.profile, base_branch),
        )  # fmt: skip

    def _token(self) -> str:
        try:
            token = self.secrets.get(GITHUB_TOKEN)
        except SecretStoreUnavailable as exc:
            raise BoardPrError(f"secure storage unavailable: {exc}") from exc
        if not token:
            raise BoardPrError("no GitHub token saved; add one in Settings first")
        return token

    def _save(
        self,
        task_id: int,
        action: str,
        board_path: str,
        branch: str,
        head: str,
        state: str,
        number: int | None = None,
        url: str | None = None,
    ) -> None:
        with Session(self.engine) as s:
            row = s.scalars(
                select(JobBoardClaim).where(
                    JobBoardClaim.task_id == task_id, JobBoardClaim.action == action
                )
            ).first()
            if row is None:
                row = JobBoardClaim(
                    task_id=task_id, action=action, board_path=board_path, branch=branch,
                    head_sha=head,
                )  # fmt: skip
                s.add(row)
            row.state, row.head_sha, row.updated_at = state, head, utcnow()
            if number is not None:
                row.pr_number, row.pr_url = number, url
            s.add(
                Event(
                    task_id=task_id,
                    kind=f"job_board.{action}.{state}",
                    detail=json.dumps({"branch": branch, "head": head[:12], "pr": url}),
                )
            )
            s.commit()


@dataclass(frozen=True)
class _Ctx:
    ppath: Path
    board_path: str
    base_ref: str
    base_sha: str
    base_branch: str
    owner: str
    repo: str
    protected: list[str]


def _released(row_line: str) -> bool:
    cells = [c.strip("*_` ").lower() for c in row_line.strip().strip("|").split("|")]
    return any(c in RELEASED for c in cells)


def _d(info: ClaimInfo | None) -> dict[str, Any] | None:
    return info.as_dict() if info else None


def _info(r: JobBoardClaim) -> ClaimInfo:
    return ClaimInfo(r.task_id, r.action, r.state, r.branch, r.head_sha, r.pr_number, r.pr_url,
                     r.board_path)  # fmt: skip
