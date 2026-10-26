"""The acceptance gate (WP10; MASTER_PROMPT sections 11 and 13, scenarios 14, 15, 16, 20).

`GateService.run(task_id)` judges a task in TESTING and is the ONLY code path that can move a
task to COMPLETED (`TaskService.transition` demands its evidence). In order, cheapest and safest
first: acceptance criteria exist -> the worker changed something -> no unexpected file (outside the
declared scope, git metadata, hooks/MCP settings, symlinks, submodules, big binaries) -> the
project's tests pass (trusted workspace only) -> the result merges cleanly onto the base ->
an independent reviewer, given only the diff, marks every criterion met with no blocking issue.

Outcomes: a *correction* sends the task back to a worker with the findings (bounded: a maximum
number of attempts and an early stop when the same failure repeats); *failed* ends the task as
FAILED; *waiting* leaves the task in TESTING and says what a person must do. Nothing here can mark
a task done because a worker or a model said so (scenario 20), and nothing here writes to the
managed project's main tree, merges, rebases, resets or cleans (scenario 16).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from cpm.db.models import Event, GateResult, Project, Task
from cpm.db.settings_repo import get_setting
from cpm.discovery.commands import detect_commands
from cpm.lanes.registry import ChangedFile
from cpm.lanes.service import LaneContext, LaneError, LaneService
from cpm.profiles import ProfileError, load_profile
from cpm.review import gitops
from cpm.review.reviewer import (
    ReviewerSource,
    ReviewerUnavailable,
    ReviewInput,
    ReviewVerdict,
    parse_verdict,
)
from cpm.review.testrun import SubprocessTestRunner, TestOutcome, TestRunner
from cpm.security.paths import PathPolicyError, resolve_in_any
from cpm.security.redaction import redact
from cpm.tasks.service import IllegalTransition, TaskData, TaskError, TaskService
from cpm.workers.permissions import _protected
from cpm.workers.supervisor import RunSupervisor
from cpm.workers.worktree import WorktreeError, WorktreeManager

log = logging.getLogger("cpm.review")

ACTOR = "acceptance-gate"
DEFAULT_MAX_CORRECTIONS = 2
MAX_CORRECTIONS_LIMIT = 5
DEFAULT_TEST_TIMEOUT_S = 900.0
MAX_FEEDBACK = 6000
MAX_SUMMARY_FILES = 300
_DIGITS = re.compile(r"\d+")


class GateError(TaskError):
    """The gate cannot run now. `.status` is the HTTP status."""

    status = 409


class GateBusy(GateError):
    pass


@dataclass
class GateOutcome:
    id: int
    task_id: int
    outcome: str
    action: str  # completed | correction | failed | waiting
    task_status: str
    message: str
    summary: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Ctx:
    task: TaskData
    project_path: Path
    slug: str
    trusted: bool
    profile: str | None
    worktree: Path
    branch: str
    base_ref: str
    base_sha: str
    snap: gitops.Snapshot


def classify(change: gitops.Change, scope: list[str], large_ok: bool = False) -> str | None:
    """Why a changed file is unexpected, or None if it is fine."""
    parts = change.path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "invalid path"
    if any(_protected(part) for part in parts):
        return "protected: git metadata, hooks or MCP settings"
    if change.mode in ("120000", "160000"):
        return "symlink or submodule"
    if scope:
        ok = any(
            change.path == e.rstrip("/") or change.path.startswith(e.rstrip("/") + "/")
            for e in scope
        )
        if not ok:
            return "outside the declared scope"
    if change.size > gitops.LARGE_FILE_BYTES and not large_ok:
        return "large binary or file (project LFS policy)"
    return None


def fingerprint(outcome: str, key: str) -> str:
    return hashlib.sha256(f"{outcome}|{key}".encode()).hexdigest()[:32]


class GateService:
    def __init__(
        self,
        engine: Engine,
        supervisor: RunSupervisor,
        worktrees: WorktreeManager,
        reviewers: ReviewerSource | None = None,
        runner: TestRunner | None = None,
        lanes: LaneService | None = None,
    ) -> None:
        self.engine = engine
        self.lanes = lanes or LaneService(engine)
        self.supervisor = supervisor
        self.worktrees = worktrees
        self.reviewers = reviewers or ReviewerSource()
        self.runner: TestRunner = runner or SubprocessTestRunner()
        self.tasks = TaskService(engine)
        self._mu = threading.Lock()
        self._running: set[int] = set()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ settings
    def max_corrections(self) -> int:
        raw = get_setting(self.engine, "gate.max_corrections", DEFAULT_MAX_CORRECTIONS)
        n = raw if isinstance(raw, int) and not isinstance(raw, bool) else DEFAULT_MAX_CORRECTIONS
        return max(0, min(n, MAX_CORRECTIONS_LIMIT))

    def auto_enabled(self) -> bool:
        return get_setting(self.engine, "gate.auto", True) is not False

    # ------------------------------------------------------------------ entry points
    def is_running(self, task_id: int) -> bool:
        with self._mu:
            return task_id in self._running

    def run(self, task_id: int) -> GateOutcome:
        with self._mu:
            if task_id in self._running:
                raise GateBusy(f"the gate is already running for task {task_id}")
            self._running.add(task_id)
        try:
            return self._run(task_id)
        finally:
            with self._mu:
                self._running.discard(task_id)

    def auto_run_pending(self) -> list[int]:
        """Start the gate (background threads) for tasks that reached TESTING and were not judged.

        Called by the scheduler tick. A task is pending when it is TESTING, has no worker or gate,
        and its last gate result is older than its last change (re-entering TESTING after a
        correction, or editing the criteria, makes it pending again).
        """
        if not self.auto_enabled():
            return []
        started: list[int] = []
        with Session(self.engine) as s:
            rows = s.execute(
                select(Task.id, Task.updated_at).where(Task.status == "TESTING").order_by(Task.id)
            ).all()
            last = {
                tid: ts
                for tid, ts in s.execute(
                    select(GateResult.task_id, GateResult.created_at).order_by(GateResult.id)
                )
            }
        for tid, updated in rows:
            if self.supervisor.busy(tid) or self.is_running(tid):
                continue
            done = last.get(tid)
            if done is not None and _naive(done) >= _naive(updated):
                continue
            t = threading.Thread(
                target=self._safe_run, args=(tid,), name=f"gate-{tid}", daemon=True
            )
            self._threads = [x for x in self._threads if x.is_alive()] + [t]
            t.start()
            started.append(tid)
        return started

    def wait_idle(self, timeout_s: float = 30.0) -> None:
        for t in list(self._threads):
            t.join(timeout_s)

    def _safe_run(self, task_id: int) -> None:
        try:
            self.run(task_id)
        except GateError as exc:
            log.info("gate for task %s did not run: %s", task_id, exc)
        except Exception:  # never let a gate thread die loudly; the task stays TESTING
            log.exception("gate for task %s crashed", task_id)

    # ------------------------------------------------------------------ evaluation
    def _run(self, task_id: int) -> GateOutcome:
        task = self.tasks.get(task_id)
        if task.status != "TESTING":
            raise GateError(f"the gate judges TESTING tasks; task {task_id} is {task.status}")
        if self.supervisor.busy(task_id):
            raise GateError("the task still has a running worker")
        try:
            ctx = self._context(task)
        except (gitops.GitOpError, WorktreeError, PathPolicyError) as exc:
            return self._waiting(
                task,
                "error",
                None,
                {"reason": redact(str(exc))[:300]},
                f"could not read the task's worktree: {redact(str(exc))[:200]}",
            )
        summary: dict[str, Any] = {
            "base": ctx.snap.base[:12],
            "head": ctx.snap.head[:12],
            "tree": ctx.snap.tree,
            "branch": ctx.branch,
            "base_ref": ctx.base_ref,
            "files": _file_rows(ctx.snap, task.declared_scope),
            "truncated": ctx.snap.truncated,
        }

        if not task.acceptance:
            summary["reason"] = "the task has no acceptance criteria"
            return self._waiting(
                task,
                "criteria_missing",
                ctx.snap,
                summary,
                "add at least one acceptance criterion to the task, then run the gate again",
            )
        if not ctx.snap.changes:
            summary["reason"] = "the worker changed nothing"
            return self._fail(
                task,
                "TESTING",
                "no_changes",
                ctx.snap,
                summary,
                "the worker finished without changing any file",
            )

        try:
            lane = self.lanes.context(task.project_id, task.lane)
        except LaneError as exc:
            summary["reason"] = str(exc)
            return self._waiting(
                task, "lane_error", ctx.snap, summary, f"{exc}; fix the profile or the task's lane"
            )
        unexpected = [
            {"path": c.path, "why": why}
            for c in ctx.snap.changes
            if (
                why := classify(
                    c,
                    task.declared_scope,
                    large_ok=lane is not None and self.lanes.allows_large(lane, c.path, c.size),
                )
            )
        ]
        summary["unexpected"] = unexpected[:MAX_SUMMARY_FILES]
        if unexpected:
            fb = "These files must not be part of the change; remove or revert them:\n" + "\n".join(
                f"- {u['path']} ({u['why']})" for u in unexpected[:50]
            )
            return self._correct(
                task,
                "TESTING",
                "unexpected_files",
                ctx.snap,
                summary,
                fb,
                fingerprint("unexpected_files", "|".join(sorted(u["path"] for u in unexpected))),
            )

        if lane is not None:
            blocked = self._lane_step(task, lane, ctx, summary)
            if blocked is not None:
                return blocked

        tests = self._run_tests(ctx)
        summary["tests"] = tests
        if tests["status"] == "needs_trust":
            return self._waiting(
                task,
                "needs_trust",
                ctx.snap,
                summary,
                "the project has test commands but its workspace is not trusted; trust it "
                "(project settings) or approve after your own check",
            )
        if tests["status"] == "failed":
            fb = "The project's tests failed:\n" + "\n".join(
                f"$ {' '.join(r['argv'])}\n{r['tail'][-1500:]}"
                for r in tests["runs"]
                if r["exit_code"] != 0
            )
            key = _DIGITS.sub("#", "".join(r["tail"][-300:] for r in tests["runs"]))
            return self._correct(
                task,
                "TESTING",
                "tests_failed",
                ctx.snap,
                summary,
                fb,
                fingerprint("tests_failed", key),
            )

        conflicts = self._conflicts(ctx)
        summary["conflicts"] = conflicts
        if conflicts:
            return self._waiting(
                task,
                "conflict",
                ctx.snap,
                summary,
                "the result no longer merges cleanly onto the base branch: "
                + ", ".join(conflicts[:5])
                + ". Rebase it on the base in your own tree (CPM never rebases).",
            )

        try:
            reviewer = self.reviewers.for_billing(task.billing_mode)
        except ReviewerUnavailable as exc:
            summary["review"] = {"status": "unavailable", "reason": str(exc)}
            return self._waiting(task, "review_pending", ctx.snap, summary, str(exc))
        try:
            self.tasks.transition(
                task.id,
                "REVIEWING",
                actor=ACTOR,
                expect_from="TESTING",
                reason="independent review",
            )
        except IllegalTransition as exc:
            raise GateError(str(exc)) from exc
        verdict = self._review(ctx, reviewer)
        summary["review"] = _verdict_dict(verdict)
        if not verdict.approved:
            fb = _review_feedback(verdict)
            key = "|".join(sorted(verdict.unmet) + sorted(i.text for i in verdict.blockers))
            return self._correct(
                task,
                "REVIEWING",
                "review_rejected",
                ctx.snap,
                summary,
                fb,
                fingerprint("review_rejected", key),
            )
        return self._complete(task, "REVIEWING", ctx.snap, summary, "independent review passed")

    # ------------------------------------------------------------------ steps
    def _lane_step(
        self, task: TaskData, lane: LaneContext, ctx: _Ctx, summary: dict[str, Any]
    ) -> GateOutcome | None:
        """The lane's artifact policy, validators and report format. None = they passed."""
        changes = [ChangedFile(c.path, c.status, c.size, c.mode) for c in ctx.snap.changes]
        report = self.lanes.report_text(self.supervisor, task.id)
        check = self.lanes.check(lane, ctx.worktree, changes, report)
        summary["lane"] = {"id": lane.lane_id, **check.as_dict()}
        if check.status == "passed":
            return None
        if check.status == "waiting":
            if check.unavailable:
                msg = f"a lane check could not run on this PC: {check.unavailable}"
            else:
                msg = (
                    "binary files need your decision before they are committed (project binary "
                    "policy): " + ", ".join(check.decisions[:5])
                )
            return self._waiting(task, "lane_waiting", ctx.snap, summary, msg)
        errors = (f for f in check.findings if f.severity == "error")
        key = "|".join(sorted(f"{f.code}:{f.path}" for f in errors))
        return self._correct(
            task,
            "TESTING",
            "lane_failed",
            ctx.snap,
            summary,
            check.feedback(),
            fingerprint("lane_failed", key),
        )

    def _context(self, task: TaskData) -> _Ctx:
        with Session(self.engine) as s:
            project = s.get(Project, task.project_id)
            if project is None:
                raise GateError("project not found")
            ppath, slug = Path(project.path), project.slug
            trusted, profile = bool(project.workspace_trusted), project.profile
        ppath = resolve_in_any(get_setting(self.engine, "approved_roots", []) or [], ppath)
        wt = self.worktrees.path_for(slug, task.id)
        if not wt.exists():
            raise GateError("the task has no worktree (nothing to review)")
        self.worktrees.verify(ppath, wt)
        base_ref = self.worktrees._base_ref(ppath)
        base_sha = gitops.resolve_base(ppath, base_ref)
        snap = gitops.snapshot(wt, base_sha)
        branch, _head = gitops.branch_head(wt)
        return _Ctx(task, ppath, slug, trusted, profile, wt, branch, base_ref, base_sha, snap)

    def _test_commands(self, ctx: _Ctx) -> list[list[str]]:
        if ctx.profile:
            try:
                cmds = load_profile(ctx.profile).test_commands
            except ProfileError:
                cmds = []
            if cmds:
                return [list(c) for c in cmds]
        return [list(c.argv) for c in detect_commands(ctx.worktree) if c.purpose == "test"]

    def _run_tests(self, ctx: _Ctx) -> dict[str, Any]:
        cmds = self._test_commands(ctx)
        if not cmds:
            return {
                "status": "skipped",
                "reason": "no test command known for this project",
                "runs": [],
            }
        if not ctx.trusted:
            return {"status": "needs_trust", "runs": [], "commands": [" ".join(c) for c in cmds]}
        timeout = float(get_setting(self.engine, "gate.test_timeout_s", DEFAULT_TEST_TIMEOUT_S))
        runs: list[dict[str, Any]] = []
        for argv in cmds:
            out: TestOutcome = self.runner.run(argv, ctx.worktree, timeout)
            runs.append(
                {
                    "argv": argv,
                    "exit_code": out.exit_code,
                    "timed_out": out.timed_out,
                    "duration_s": out.duration_s,
                    "tail": out.tail[-3000:],
                }
            )
            if not out.passed:
                break  # the first failing command is enough feedback
        ok = all(r["exit_code"] == 0 and not r["timed_out"] for r in runs)
        return {"status": "passed" if ok else "failed", "runs": runs}

    def _conflicts(self, ctx: _Ctx) -> list[str]:
        try:
            probe = gitops.commit_tree_probe(ctx.worktree, ctx.snap)
            return gitops.merge_conflicts(ctx.worktree, ctx.base_sha, probe)
        except gitops.GitOpError as exc:  # old git or odd repo: refuse rather than assume clean
            return [f"(could not check: {redact(str(exc))[:120]})"]

    def _review(self, ctx: _Ctx, reviewer: Any) -> ReviewVerdict:
        diff, cut = gitops.file_diff(ctx.worktree, ctx.snap)
        data = ReviewInput(
            title=ctx.task.title,
            description=ctx.task.description,
            criteria=list(ctx.task.acceptance),
            files=[c.path for c in ctx.snap.changes],
            diff=diff,
            diff_truncated=cut,
        )
        try:
            raw = reviewer.review(data)
        except Exception as exc:  # a crashing reviewer must not read as approval
            raw = {
                "issues": [
                    {
                        "severity": "blocker",
                        "category": "other",
                        "text": f"reviewer failed: {type(exc).__name__}",
                    }
                ]
            }
        return parse_verdict(raw, data.criteria)

    # ------------------------------------------------------------------ recording / outcomes
    def _record(
        self,
        task: TaskData,
        outcome: str,
        action: str,
        snap: gitops.Snapshot | None,
        summary: dict[str, Any],
        fp: str = "",
    ) -> int:
        body = redact(json.dumps(summary, default=str))[:120_000]
        with Session(self.engine) as s:
            row = GateResult(
                task_id=task.id,
                outcome=outcome,
                action=action,
                head_sha=snap.head if snap else None,
                base_sha=snap.base if snap else None,
                fingerprint=fp,
                summary_json=body,
            )
            s.add(row)
            s.add(
                Event(
                    project_id=task.project_id,
                    task_id=task.id,
                    kind="gate.result",
                    detail=json.dumps({"outcome": outcome, "action": action}),
                )
            )
            s.commit()
            return int(row.id)

    def _out(
        self,
        gid: int,
        task: TaskData,
        outcome: str,
        action: str,
        message: str,
        summary: dict[str, Any],
    ) -> GateOutcome:
        status = self.tasks.get(task.id).status
        return GateOutcome(gid, task.id, outcome, action, status, message, summary)

    def _waiting(
        self,
        task: TaskData,
        outcome: str,
        snap: gitops.Snapshot | None,
        summary: dict[str, Any],
        message: str,
    ) -> GateOutcome:
        gid = self._record(task, outcome, "waiting", snap, summary)
        return self._out(gid, task, outcome, "waiting", message, summary)

    def _fail(
        self,
        task: TaskData,
        src: str,
        outcome: str,
        snap: gitops.Snapshot | None,
        summary: dict[str, Any],
        message: str,
    ) -> GateOutcome:
        try:
            self.tasks.transition(
                task.id, "FAILED", actor=ACTOR, reason=message[:300], expect_from=src
            )
        except IllegalTransition as exc:
            raise GateError(str(exc)) from exc
        gid = self._record(task, outcome, "failed", snap, summary)
        return self._out(gid, task, outcome, "failed", message, summary)

    def _complete(
        self,
        task: TaskData,
        src: str,
        snap: gitops.Snapshot,
        summary: dict[str, Any],
        message: str,
        *,
        by: str = "gate",
    ) -> GateOutcome:
        evidence = {"acceptance_gate": "passed", "tree": snap.tree, "by": by}
        try:
            self.tasks.transition(
                task.id,
                "COMPLETED",
                actor=ACTOR,
                reason=message,
                evidence=evidence,
                expect_from=src,
            )
        except IllegalTransition as exc:
            raise GateError(str(exc)) from exc
        gid = self._record(task, "passed", "completed", snap, summary)
        return self._out(gid, task, "passed", "completed", message, summary)

    def _history(self, task_id: int) -> list[tuple[str, str]]:
        with Session(self.engine) as s:
            rows = s.execute(
                select(GateResult.action, GateResult.fingerprint)
                .where(
                    GateResult.task_id == task_id, GateResult.action.in_(("correction", "failed"))
                )
                .order_by(GateResult.id.desc())
            ).all()
        return [(a, f) for a, f in rows]

    def _correct(
        self,
        task: TaskData,
        src: str,
        outcome: str,
        snap: gitops.Snapshot,
        summary: dict[str, Any],
        feedback: str,
        fp: str,
    ) -> GateOutcome:
        """Send the findings back to a worker, or stop when the bounds are reached."""
        hist = self._history(task.id)
        used = sum(1 for a, _ in hist if a == "correction")
        limit = self.max_corrections()
        repeated = bool(hist) and hist[0] == ("correction", fp)
        if used >= limit or repeated:
            why = (
                f"the same failure ({outcome}) came back after a correction"
                if repeated
                else f"correction limit reached ({limit}): {outcome}"
            )
            summary["gave_up"] = why
            return self._fail(task, src, outcome, snap, summary, why)
        fb = redact(feedback)[:MAX_FEEDBACK]
        try:
            self.tasks.transition(
                task.id,
                "CORRECTING",
                actor=ACTOR,
                reason=f"{outcome}: correction {used + 1} of {limit}",
                expect_from=src,
            )
        except IllegalTransition as exc:
            raise GateError(str(exc)) from exc
        summary["feedback"] = fb
        summary["correction"] = {"attempt": used + 1, "of": limit}
        gid = self._record(task, outcome, "correction", snap, summary, fp)
        msg = f"{outcome}: correction {used + 1} of {limit} started"
        try:
            self.supervisor.start(task.id, feedback=fb)
        except Exception as exc:  # budget, parallel limit, worktree trouble: a person decides
            reason = f"correction could not start: {redact(str(exc))[:200]}"
            try:
                self.tasks.transition(
                    task.id, "PAUSED", actor=ACTOR, reason=reason, expect_from="CORRECTING"
                )
            except IllegalTransition:
                pass
            msg = reason
        return self._out(gid, task, outcome, "correction", msg, summary)

    # ------------------------------------------------------------------ human approval
    def approve(self, task_id: int, gate_id: int) -> GateOutcome:
        """A person accepts a result the gate could not judge by itself (no live reviewer).

        Only for a `review_pending` result that is still the latest and whose tree is unchanged;
        tests, scope and merge checks have already passed at that point.
        """
        with self._mu:
            if task_id in self._running:
                raise GateBusy("the gate is running; wait for it to finish")
            self._running.add(task_id)
        try:
            task = self.tasks.get(task_id)
            with Session(self.engine) as s:
                last = s.scalars(
                    select(GateResult)
                    .where(GateResult.task_id == task_id)
                    .order_by(GateResult.id.desc())
                    .limit(1)
                ).first()
                if last is not None:
                    s.expunge(last)
            if last is None or last.id != gate_id or last.outcome != "review_pending":
                raise GateError("only the latest 'review pending' result can be approved")
            if task.status != "TESTING" or self.supervisor.busy(task_id):
                raise GateError(f"task is {task.status}; nothing to approve")
            ctx = self._context(task)
            summary = json.loads(last.summary_json)
            if summary.get("tree") != ctx.snap.tree:
                raise GateError(
                    "the work changed since the review was requested; run the gate again"
                )
            summary["review"] = {"status": "approved by a person"}
            self.tasks.transition(
                task.id, "REVIEWING", actor=ACTOR, expect_from="TESTING", reason="person review"
            )
            return self._complete(
                task,
                "REVIEWING",
                ctx.snap,
                summary,
                "approved by the user after reading the diff",
                by="user",
            )
        except (gitops.GitOpError, WorktreeError, PathPolicyError) as exc:
            raise GateError(redact(str(exc))[:200]) from exc
        finally:
            with self._mu:
                self._running.discard(task_id)

    # ------------------------------------------------------------------ read side
    def changes(self, task_id: int) -> dict[str, Any]:
        """What the task's worktree changes right now (read-only; safe while a worker runs)."""
        task = self.tasks.get(task_id)
        ctx = self._guarded_context(task)
        files = _file_rows(ctx.snap, task.declared_scope)
        return {
            "branch": ctx.branch,
            "base_ref": ctx.base_ref,
            "base": ctx.snap.base[:12],
            "head": ctx.snap.head[:12],
            "tree": ctx.snap.tree,
            "files": files,
            "unexpected": sum(1 for f in files if f["unexpected"]),
            "truncated": ctx.snap.truncated,
        }

    def diff(self, task_id: int, path: str | None = None) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        ctx = self._guarded_context(task)
        try:
            text, cut = gitops.file_diff(ctx.worktree, ctx.snap, path)
        except gitops.GitOpError as exc:
            raise GateError(redact(str(exc))[:200]) from exc
        return {"path": path, "diff": text, "truncated": cut}

    def _guarded_context(self, task: TaskData) -> _Ctx:
        try:
            return self._context(task)
        except (gitops.GitOpError, WorktreeError, PathPolicyError) as exc:
            raise GateError(f"cannot read the worktree: {redact(str(exc))[:200]}") from exc

    def results(self, task_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with Session(self.engine) as s:
            rows = s.scalars(
                select(GateResult)
                .where(GateResult.task_id == task_id)
                .order_by(GateResult.id.desc())
                .limit(limit)
            ).all()
            return [
                {
                    "id": r.id,
                    "outcome": r.outcome,
                    "action": r.action,
                    "at": r.created_at.isoformat(),
                    "summary": json.loads(r.summary_json),
                }
                for r in rows
            ]


def _naive(ts: datetime) -> datetime:
    return ts.replace(tzinfo=None) if ts.tzinfo else ts


def _file_rows(snap: gitops.Snapshot, scope: list[str]) -> list[dict[str, Any]]:
    rows = []
    for c in snap.changes[:MAX_SUMMARY_FILES]:
        rows.append(
            {
                "path": c.path,
                "status": c.status,
                "added": c.added,
                "deleted": c.deleted,
                "size": c.size,
                "unexpected": classify(c, scope),
            }
        )
    return rows


def _verdict_dict(v: ReviewVerdict) -> dict[str, Any]:
    return {
        "status": "approved" if v.approved else "rejected",
        "summary": v.summary,
        "criteria": v.criteria,
        "issues": [asdict(i) for i in v.issues],
    }


def _review_feedback(v: ReviewVerdict) -> str:
    lines = ["An independent review rejected the result."]
    for c in v.criteria:
        if not c["met"]:
            lines.append(f"- acceptance criterion not met: {c['text']} ({c['note']})")
    for i in v.blockers:
        lines.append(f"- blocking issue [{i.category}]: {i.text}")
    return "\n".join(lines)
