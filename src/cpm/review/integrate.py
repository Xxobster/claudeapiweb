"""`pr_only` integration of a COMPLETED task (WP10; Nirandyr rules 37-40, MASTER_PROMPT section 13).

Steps, each persisted so a retry resumes where it stopped and never repeats a finished one:
commit the gated tree on the task branch -> re-validate that it still merges onto the base ->
push `claude/*` (never a protected branch, never forced) -> open the pull request -> task
INTEGRATED. CPM never merges, rebases, resets, cleans or touches the managed project's main
tree; the merge is the person's click on GitHub. Token: secret store only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from cpm.db.models import Event, GateResult, Integration, Project, utcnow
from cpm.db.settings_repo import get_setting
from cpm.discovery.gitinfo import GitUnavailableError, read_remotes
from cpm.profiles import ProfileError, load_profile
from cpm.review import gitops
from cpm.review.gate import GateError
from cpm.review.github import (
    GITHUB_TOKEN,
    GitHubClient,
    GitHubError,
    HttpGitHubClient,
    parse_remote,
)
from cpm.security.paths import PathPolicyError, resolve_in_any
from cpm.security.redaction import redact
from cpm.security.secrets_store import SecretStore, SecretStoreUnavailable
from cpm.tasks.service import IllegalTransition, TaskService
from cpm.workers.worktree import WorktreeError, WorktreeManager

log = logging.getLogger("cpm.review")
ACTOR = "integration"
MAX_NEEDS_PC = 2000


class IntegrationError(GateError):
    pass


@dataclass
class IntegrationInfo:
    task_id: int
    state: str  # committed | pushed | pr_open
    branch: str
    head_sha: str
    pr_number: int | None
    pr_url: str | None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def pr_body(task_title: str, summary: dict[str, Any], needs_pc: str) -> str:
    """Sections that Nirandyr rules 37-40 require; all four are always present."""
    files = summary.get("files", [])
    review = summary.get("review", {}) or {}
    tests = summary.get("tests", {}) or {}
    sec = [i for i in review.get("issues", []) if i.get("category") == "security"]
    lines = [f"Task: {task_title}", ""]
    lines += ["## Files changed", ""]
    lines += [
        f"- `{f['path']}` ({f['status']}, +{f.get('added') or 0}/-{f.get('deleted') or 0})"
        for f in files[:200]
    ] or ["- (none)"]
    lines += ["", "## How it was tested", ""]
    if tests.get("runs"):
        for r in tests["runs"]:
            lines.append(f"- `{' '.join(r['argv'])}` exit {r['exit_code']} in {r['duration_s']} s")
    else:
        lines.append(f"- tests: {tests.get('status', 'not run')} ({tests.get('reason', '')})")
    lines.append(f"- independent review: {review.get('status', 'not run')}")
    lines += ["", "## Security findings", ""]
    lines += [f"- {i['text']}" for i in sec] or [
        "- None reported by the automated review (not a substitute for a human review)."
    ]
    lines += ["", "## Exploit findings", ""]
    lines += [
        "- None found by the automated checks (scope, protected paths, symlinks, "
        "binary size). A person should still look at the diff."
    ]
    lines += [
        "",
        "## What still needs the PC",
        "",
        needs_pc.strip()[:MAX_NEEDS_PC] or "Nothing recorded by CPM; check before merging.",
    ]
    lines += [
        "",
        "_Opened by Claude Project Manager. CPM never merges: review and merge on GitHub._",
    ]
    return "\n".join(lines)


class IntegrationService:
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
    def get(self, task_id: int) -> IntegrationInfo | None:
        with Session(self.engine) as s:
            row = s.scalars(select(Integration).where(Integration.task_id == task_id)).first()
            return _info(row) if row else None

    # ------------------------------------------------------------------ run
    def integrate(self, task_id: int, needs_pc: str = "") -> IntegrationInfo:
        task = self.tasks.get(task_id)
        existing = self.get(task_id)
        if existing and existing.state == "pr_open":
            return existing
        if task.status != "COMPLETED":
            raise IntegrationError(
                f"only a COMPLETED task can be integrated; this one is {task.status}"
            )
        with Session(self.engine) as s:
            project = s.get(Project, task.project_id)
            gate = s.scalars(
                select(GateResult)
                .where(GateResult.task_id == task_id, GateResult.outcome == "passed")
                .order_by(GateResult.id.desc())
                .limit(1)
            ).first()
            if project is None or gate is None:
                raise IntegrationError("no passed acceptance gate on record for this task")
            s.expunge_all()
        if project.integration_mode != "pr_only":
            raise IntegrationError("only the pr_only integration mode exists")
        summary = json.loads(gate.summary_json)
        try:
            return self._integrate(task, project, gate, summary, existing, needs_pc)
        except (gitops.GitOpError, WorktreeError, PathPolicyError, GitUnavailableError) as exc:
            raise IntegrationError(redact(str(exc))[:300]) from exc

    def _integrate(
        self,
        task: Any,
        project: Project,
        gate: GateResult,
        summary: dict[str, Any],
        existing: IntegrationInfo | None,
        needs_pc: str,
    ) -> IntegrationInfo:
        ppath = resolve_in_any(
            get_setting(self.engine, "approved_roots", []) or [], Path(project.path)
        )
        wt = self.worktrees.path_for(project.slug, task.id)
        if not wt.exists():
            raise IntegrationError("the task's worktree is gone; cannot integrate")
        self.worktrees.verify(ppath, wt)
        remotes = read_remotes(ppath)
        remote_url = remotes.get("origin")
        target = parse_remote(remote_url)
        if target is None:
            raise IntegrationError("pull requests need an 'origin' remote on github.com")
        token = self._token()
        base_ref = self.worktrees._base_ref(ppath)
        base_sha = gitops.resolve_base(ppath, base_ref)
        base_branch = gitops.default_branch_name(ppath, base_ref)
        protected = _protected_branches(project.profile, base_branch)
        branch, _ = gitops.branch_head(wt)
        gitops.guard_push(branch, protected)

        # 1. commit exactly the gated tree
        expect = str(summary.get("tree") or "")
        if existing is None:
            head = gitops.commit_all(
                wt, branch, f"{task.title}\n\nCPM task #{task.id}", expect_tree=expect
            )
            self._save(task.id, gate.id, branch, head, "committed")
        else:
            head = existing.head_sha
            if gitops.branch_head(wt)[1] != head:
                raise IntegrationError(
                    "the branch moved since it was committed; stop and review it"
                )
        # 2. re-validate against the CURRENT base, right before publishing
        snap = gitops.snapshot(wt, base_sha)
        conflicts = gitops.merge_conflicts(wt, base_sha, head)
        if conflicts:
            raise IntegrationError(
                "the branch no longer merges cleanly onto "
                + base_branch
                + ": "
                + ", ".join(conflicts[:5])
                + ". Rebase it in your own tree; CPM will not."
            )
        if not snap.changes:
            raise IntegrationError("nothing differs from the base branch; nothing to publish")
        # 3. push the task branch (never main, never forced)
        info = self.get(task.id)
        assert info is not None  # noqa: S101 (saved above)
        if info.state == "committed":
            gitops.push_branch(ppath, branch, "origin", token, protected)
            self._save(task.id, gate.id, branch, head, "pushed")
        # 4. open the pull request
        body = pr_body(task.title, summary, needs_pc)
        try:
            pr = self.github.create_pull_request(
                target[0],
                target[1],
                head=branch,
                base=base_branch,
                title=task.title,
                body=body,
                token=token,
            )
        except GitHubError as exc:
            raise IntegrationError(f"branch pushed, pull request not opened: {exc}") from exc
        self._save(task.id, gate.id, branch, head, "pr_open", pr.number, pr.url)
        try:
            self.tasks.transition(
                task.id,
                "INTEGRATED",
                actor=ACTOR,
                expect_from="COMPLETED",
                reason=f"pull request #{pr.number}",
            )
        except IllegalTransition as exc:  # PR exists; the status will be fixed by the next call
            log.warning("task %s: PR opened but status not updated: %s", task.id, exc)
        out = self.get(task.id)
        assert out is not None  # noqa: S101
        return out

    def _token(self) -> str:
        try:
            token = self.secrets.get(GITHUB_TOKEN)
        except SecretStoreUnavailable as exc:
            raise IntegrationError(f"secure storage unavailable: {exc}") from exc
        if not token:
            raise IntegrationError("no GitHub token saved; add one in Settings first")
        return token

    def _save(
        self,
        task_id: int,
        gate_id: int,
        branch: str,
        head: str,
        state: str,
        number: int | None = None,
        url: str | None = None,
    ) -> None:
        with Session(self.engine) as s:
            row = s.scalars(select(Integration).where(Integration.task_id == task_id)).first()
            if row is None:
                row = Integration(task_id=task_id, gate_id=gate_id, branch=branch, head_sha=head)
                s.add(row)
            row.state, row.head_sha, row.updated_at = state, head, utcnow()
            if number is not None:
                row.pr_number, row.pr_url = number, url
            s.add(
                Event(
                    task_id=task_id,
                    kind="integration." + state,
                    detail=json.dumps({"branch": branch, "head": head[:12], "pr": url}),
                )
            )
            s.commit()


def _info(r: Integration) -> IntegrationInfo:
    return IntegrationInfo(r.task_id, r.state, r.branch, r.head_sha, r.pr_number, r.pr_url)


def _protected_branches(profile: str | None, base_branch: str) -> list[str]:
    names = {"main", "master", base_branch}
    if profile:
        try:
            names |= set(load_profile(profile).protected_branches)
        except ProfileError:
            pass
    return sorted(names)
