"""Restart reconciliation (WP9, scenarios 11, 12, 18; docs/THREATS.md T-22, T-41).

After CPM stopped or crashed the database can claim things that are no longer true: runs that are
"RUNNING" with no process, tasks stuck in RESERVED/RUNNING, reservations and budget holds that
nobody will release, worktrees left behind. `Recovery.reconcile()` makes the records true again
and never starts anything: no duplicate execution is possible because the only things it does to
a task are RUNNING -> FAILED/PAUSED and RESERVED -> READY, and the scheduler (a separate, later
step) is what may start work again, within its usual retry bounds.

Decision per orphaned task (a run without a live worker in this process):
- an open non-idempotent step (a `step_begin` without `step_end` in the run log, e.g. an allowed
  `git commit`) means the world may have changed half way: the task goes to PAUSED and a person
  decides (the scheduler never resumes PAUSED work);
- otherwise the task goes to FAILED ("interrupted by CPM restart"); the scheduler's bounded retry
  rules apply, and a stored session id lets the next run resume the conversation.
Worktrees are only reported, never deleted (a worktree may hold the only copy of someone's work).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from cpm.budget.ledger import BudgetLedger
from cpm.config import Settings
from cpm.db.models import Event, Project, Reservation, Run, Task, utcnow
from cpm.db.settings_repo import set_setting
from cpm.locks.reservations import ReservationService
from cpm.security.redaction import redact
from cpm.tasks import states
from cpm.tasks.service import IllegalTransition, TaskService
from cpm.workers.events import RunLog
from cpm.workers.worktree import WorktreeError, WorktreeManager

log = logging.getLogger("cpm.recovery")

ACTOR = "recovery"
REPORT_KEY = "recovery.last_report"
_UNFINISHED_RUN = ("STARTING", "RUNNING")


@dataclass
class OrphanRun:
    run_id: int
    task_id: int
    outcome: str  # "failed" | "paused" | "released"
    open_step: str | None = None
    process_killed: bool = False


@dataclass
class WorktreeNote:
    path: str
    task_id: int | None
    task_status: str | None
    dirty: int | None
    unmerged_commits: int | None
    note: str


@dataclass
class RecoveryReport:
    at: str = ""
    runs: list[OrphanRun] = field(default_factory=list)
    tasks_demoted: list[int] = field(default_factory=list)
    reservations_released: int = 0
    budget_holds_released: int = 0
    worktrees: list[WorktreeNote] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.runs or self.tasks_demoted or self.reservations_released
            or self.budget_holds_released
        )  # fmt: skip

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def open_step(log_path: Path) -> str | None:
    """Name of the last `step_begin` of a run log that has no `step_end` after it."""
    if not log_path.is_file():
        return None
    current: str | None = None
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except ValueError:
                continue  # a partially written last line
            kind = ev.get("type")
            if kind == "step_begin":
                current = str(ev.get("step") or "unknown step")
            elif kind == "step_end":
                current = None
    return current


def _kill_leftover(pid: int | None, run_id: int, run_dir: Path) -> bool:
    """Kill a worker that outlived CPM (POSIX only; on Windows the Job Object already did).

    A pid number can be reused by an unrelated program after a reboot, so the process is only
    killed when its command line is a CPM worker for exactly this run's spec file.
    """
    if not pid or pid <= 1 or not sys.platform.startswith("linux"):
        return False
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    args = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    if "cpm.workers.run" not in args or str(run_dir / "spec.json") not in args:
        return False
    try:
        os.killpg(pid, signal.SIGKILL)  # the worker leads its own session (proctree.spawn)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return False
    return True


class Recovery:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        *,
        is_live: Callable[[int], bool] = lambda _run_id: False,
        reservations: ReservationService | None = None,
        ledger: BudgetLedger | None = None,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.is_live = is_live
        self.reservations = reservations or ReservationService(engine)
        self.ledger = ledger or BudgetLedger(engine)
        self.tasks = TaskService(engine)
        self.worktrees = WorktreeManager(settings.worktree_root)

    # ------------------------------------------------------------------ public
    def reconcile(self) -> RecoveryReport:
        """Idempotent: a second call right after the first changes nothing."""
        rep = RecoveryReport(at=utcnow().isoformat())
        handled: set[int] = set()
        for run_id, task_id in self._unfinished_runs():
            if self.is_live(run_id):
                handled.add(task_id)
                continue
            rep.runs.append(self._orphan_run(run_id, task_id))
            handled.add(task_id)
        self._stuck_tasks(rep, handled)
        self._release_unowned_reservations(rep, handled)
        rep.reservations_released += self._sweep()
        live = {r for r, _ in self._unfinished_runs() if self.is_live(r)}
        rep.budget_holds_released = self.ledger.release_orphans(live)
        rep.worktrees = self._scan_worktrees()
        self._record(rep)
        return rep

    # ------------------------------------------------------------------ steps
    def _unfinished_runs(self) -> list[tuple[int, int]]:
        with Session(self.engine) as s:
            rows = s.execute(
                select(Run.id, Run.task_id).where(Run.status.in_(_UNFINISHED_RUN)).order_by(Run.id)
            ).all()
        return [(int(r), int(t)) for r, t in rows]

    def _orphan_run(self, run_id: int, task_id: int) -> OrphanRun:
        run_dir = self.settings.runs_dir / str(run_id)
        step = open_step(run_dir / "events.jsonl")
        with Session(self.engine) as s:
            r = s.get(Run, run_id)
            pid = r.pid if r else None
        killed = _kill_leftover(pid, run_id, run_dir)
        reason = "interrupted by CPM restart"
        if step:
            reason += f"; step '{step}' may be half done, check the worktree before resuming"
        with Session(self.engine) as s:
            r = s.get(Run, run_id)
            if r is not None and r.status in _UNFINISHED_RUN:
                r.status, r.ended_at = "CRASHED", utcnow()
                r.result_json = json.dumps({"reason": redact(reason)})
            t = s.get(Task, task_id)
            s.add(Event(
                project_id=t.project_id if t else None, task_id=task_id, run_id=run_id,
                kind="recovery.run_orphaned",
                detail=json.dumps({"open_step": step, "process_killed": killed}),
            ))  # fmt: skip
            s.commit()
        RunLog(run_dir / "events.jsonl").append("recovered", reason=reason, open_step=step)
        self.ledger.release_run(run_id)
        outcome = self._demote(task_id, paused=step is not None, reason=reason)
        return OrphanRun(run_id, task_id, outcome, step, killed)

    def _demote(self, task_id: int, *, paused: bool, reason: str) -> str:
        with Session(self.engine) as s:
            t = s.get(Task, task_id)
            status = t.status if t else None
        if status in ("RESERVED", "RUNNING"):
            to = "PAUSED" if paused else "FAILED"
            try:
                self.tasks.transition(task_id, to, actor=ACTOR, reason=reason, expect_from=status)
            except IllegalTransition:
                return "released"  # someone moved it meanwhile; leave it
            self.reservations.release(task_id, reason="recovery")
            return to.lower()
        self.reservations.release(task_id, reason="recovery")
        return "released"

    def _stuck_tasks(self, rep: RecoveryReport, handled: set[int]) -> None:
        """RUNNING/RESERVED tasks with no run at all (crash between the two writes)."""
        with Session(self.engine) as s:
            stuck = [
                (int(i), st)
                for i, st in s.execute(
                    select(Task.id, Task.status).where(Task.status.in_(("RESERVED", "RUNNING")))
                )
            ]
        for task_id, status in stuck:
            if task_id in handled:
                continue
            reason = "interrupted by CPM restart before a worker existed"
            try:
                to = "READY" if status == "RESERVED" else "FAILED"
                self.tasks.transition(task_id, to, actor=ACTOR, reason=reason, expect_from=status)
            except IllegalTransition:
                continue
            self.reservations.release(task_id, reason="recovery")
            rep.tasks_demoted.append(task_id)

    def _release_unowned_reservations(self, rep: RecoveryReport, handled: set[int]) -> None:
        """Reservations held by a task that is not RESERVED/RUNNING (nobody will release them)."""
        with Session(self.engine) as s:
            holders = {int(t) for t in s.scalars(select(Reservation.task_id).distinct())}
            active = {
                int(i)
                for i in s.scalars(select(Task.id).where(Task.status.in_(("RESERVED", "RUNNING"))))
            }
        for task_id in sorted(holders - active - handled):
            rep.reservations_released += self.reservations.release(task_id, reason="recovery")

    def _sweep(self) -> int:
        with Session(self.engine) as s:
            before = len(list(s.scalars(select(Reservation.id))))
        self.reservations.sweep()
        with Session(self.engine) as s:
            return before - len(list(s.scalars(select(Reservation.id))))

    def _scan_worktrees(self) -> list[WorktreeNote]:
        root = self.settings.worktree_root
        notes: list[WorktreeNote] = []
        if not root.is_dir():
            return notes
        with Session(self.engine) as s:
            slugs = {int(p.id): p.slug for p in s.scalars(select(Project))}
            status = {int(i): st for i, st in s.execute(select(Task.id, Task.status))}
        for slug_dir in sorted(
            p for p in root.iterdir() if p.is_dir() and p.name in slugs.values()
        ):
            for wt in sorted(p for p in slug_dir.iterdir() if p.is_dir()):
                task_id = (
                    int(wt.name[5:])
                    if wt.name.startswith("task-") and wt.name[5:].isdigit()
                    else None
                )
                st = status.get(task_id) if task_id is not None else None
                if st in states.ACTIVE or st == "PAUSED":
                    continue  # a worktree in use (or resumable) is not an orphan
                try:
                    dirty, ahead = self.worktrees.status(wt)
                except (WorktreeError, ValueError, OSError):
                    notes.append(WorktreeNote(str(wt), task_id, st, None, None,
                                              "could not inspect; left untouched"))  # fmt: skip
                    continue
                note = (
                    "has work that exists nowhere else; kept"
                    if dirty or ahead
                    else ("clean; can be removed from the task screen")
                )
                notes.append(WorktreeNote(str(wt), task_id, st, dirty, ahead, note))
        return notes

    def _record(self, rep: RecoveryReport) -> None:
        set_setting(self.engine, REPORT_KEY, rep.as_dict())
        if rep.changed:
            with Session(self.engine) as s:
                s.add(Event(kind="recovery.reconciled", detail=json.dumps({
                    "runs": len(rep.runs), "tasks": len(rep.tasks_demoted),
                    "reservations": rep.reservations_released,
                    "budget_holds": rep.budget_holds_released,
                })))  # fmt: skip
                s.commit()
            log.warning(
                "recovery: %d orphaned run(s), %d stuck task(s), %d reservation(s), %d hold(s)",
                len(rep.runs),
                len(rep.tasks_demoted),
                rep.reservations_released,
                rep.budget_holds_released,
            )
