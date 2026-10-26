"""The scheduler: one `tick()` that moves READY tasks to running workers, deterministically.

Per tick (`Scheduler.tick`, serialised by a lock, safe to call from a test or the API):
  1. housekeeping: heartbeat the reservations of RESERVED/RUNNING tasks, release what a task that
     left RESERVED/RUNNING still holds, demote a RESERVED task whose launch never happened,
     scan the managed projects' main trees for Desktop edits, promote BLOCKED tasks whose
     dependencies are done, retry FAILED tasks within the bounds of `policy.retry_decision`;
  2. dispatch (unless paused): READY queue in `policy.order_key` order; for each task the limits
     (global, per project, heavy), `Admission.reserve` (scope, Desktop claims, external locks,
     CPM reservations), then `RunSupervisor.start`, which performs budget admission (WP8) itself.
A task that cannot start stays READY with one visible reason (`reservation.blocked` or
`scheduler.deferred` events, written once per distinct reason). Reservations are released in a
`finally` when the launch fails and by housekeeping when a run has ended (Nirandyr rule 27).
The scheduler never starts a task that is not READY, never resumes PAUSED/PAUSED_BUDGET work
(that is a user decision) and never calls the SDK itself.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, func, insert, select
from sqlalchemy.orm import Session

from cpm.db.models import Event, Project, Reservation, Run, Task, TaskDep, utcnow
from cpm.db.settings_repo import get_setting, set_setting
from cpm.locks.admission import Admission, profile_for
from cpm.locks.external import ExternalWatcher
from cpm.locks.normalize import HEAVY_CLASS
from cpm.locks.reservations import ReservationService
from cpm.locks.types import ReservationConflict, ResourceRequest
from cpm.scheduler import policy
from cpm.scheduler.policy import Candidate, FailedRun, SchedulerConfig
from cpm.security.redaction import redact
from cpm.tasks import states
from cpm.tasks.service import IllegalTransition, TaskData, TaskError, TaskService
from cpm.workers.supervisor import MAX_LIVE, RunError, RunSupervisor

log = logging.getLogger("cpm.scheduler")

ACTOR = "scheduler"
COOLDOWN_STEPS_S = (30.0, 60.0, 120.0, 300.0)
_WAITING = "waiting for tasks"  # prefix `TaskService.mark_ready` writes for dependency blocks

ResourcesFor = Callable[[TaskData], Sequence[ResourceRequest]]
StartArgsFor = Callable[[TaskData], dict[str, Any]]


@dataclass
class TickReport:
    at: str = ""
    paused: bool = False
    started: list[int] = field(default_factory=list)
    skipped: dict[int, str] = field(default_factory=dict)
    retried: list[int] = field(default_factory=list)
    gave_up: list[int] = field(default_factory=list)
    promoted: list[int] = field(default_factory=list)
    released: list[int] = field(default_factory=list)
    demoted: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at, "paused": self.paused, "started": self.started,
            "skipped": {str(k): v for k, v in self.skipped.items()},
            "retried": self.retried, "gave_up": self.gave_up, "promoted": self.promoted,
            "released": self.released, "demoted": self.demoted, "errors": self.errors,
        }  # fmt: skip


@dataclass(frozen=True)
class _Ref:
    id: int
    project_id: int


def _aware(ts: datetime | None) -> datetime | None:
    return ts if ts is None or ts.tzinfo else ts.replace(tzinfo=UTC)


class Scheduler:
    def __init__(
        self,
        engine: Engine,
        supervisor: RunSupervisor,
        admission: Admission,
        reservations: ReservationService,
        watcher: ExternalWatcher | None = None,
        *,
        resources_for: ResourcesFor | None = None,
        start_args_for: StartArgsFor | None = None,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.supervisor = supervisor
        self.admission = admission
        self.reservations = reservations
        self.watcher = watcher or ExternalWatcher(engine)
        self.tasks = TaskService(engine)
        self.resources_for = resources_for or (lambda _t: ())  # WP12 lanes add their resources
        self.start_args_for = start_args_for or (lambda _t: {})
        self.clock = clock
        self.monotonic = monotonic
        self._tick_lock = threading.Lock()
        self._cooldown: dict[int, tuple[float, int]] = {}  # task -> (until, failures)
        self._last_scan: dict[int, float] = {}
        self._last: TickReport | None = None
        self._started = False

    # ----------------------------------------------------------------- settings
    def config(self) -> SchedulerConfig:
        return SchedulerConfig.load(get_setting(self.engine, policy.SETTING_CONFIG))

    def set_config(self, raw: dict[str, Any]) -> SchedulerConfig:
        cfg = SchedulerConfig.load(raw)
        set_setting(self.engine, policy.SETTING_CONFIG, cfg.dump())
        return cfg

    def paused(self) -> bool:
        return bool(get_setting(self.engine, policy.SETTING_PAUSED, False))

    def set_paused(self, value: bool) -> None:
        set_setting(self.engine, policy.SETTING_PAUSED, bool(value))

    def global_limit(self) -> int:
        wiz = get_setting(self.engine, "wizard.config") or {}
        try:
            n = int(wiz.get("max_parallel_agents", 3))
        except (TypeError, ValueError):
            n = 3
        return max(1, min(n, MAX_LIVE))

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Startup: expire lapsed reservations whose holder is gone (recovery proper is WP9)."""
        if not self._started:
            self._started = True
            self.reservations.sweep()

    def status(self) -> dict[str, Any]:
        cfg = self.config()
        running = self._in_flight()
        with Session(self.engine) as s:
            ready = s.scalar(select(func.count()).where(Task.status == "READY"))
        return {
            "paused": self.paused(),
            "limits": {
                "global": self.global_limit(), "per_project": cfg.per_project_limit,
                "heavy": cfg.heavy_limit,
            },
            "running": [{"task_id": t, "project_id": p} for t, p in running],
            "ready": int(ready or 0),
            "config": cfg.dump(),
            "last_tick": self._last.as_dict() if self._last else None,
        }  # fmt: skip

    # ----------------------------------------------------------------- events
    def _event_once(self, task: TaskData | _Ref, kind: str, **detail: Any) -> bool:
        """Write a scheduler event unless the task's last scheduler event is identical."""
        body = redact(json.dumps(detail, sort_keys=True, default=str))
        with Session(self.engine) as s:
            last = s.scalars(
                select(Event)
                .where(Event.task_id == task.id, Event.kind.like("scheduler.%"))
                .order_by(Event.id.desc())
                .limit(1)
            ).first()
            if last is not None and last.kind == kind and last.detail == body:
                return False
            s.execute(
                insert(Event).values(
                    ts=self.clock(), project_id=task.project_id, task_id=task.id,
                    kind=kind, detail=body,
                )
            )  # fmt: skip
            s.commit()
        return True

    # ----------------------------------------------------------------- tick
    def tick(self) -> TickReport:
        report = TickReport(at=self.clock().isoformat())
        if not self._tick_lock.acquire(blocking=False):
            report.errors.append("another tick is running")
            return report
        try:
            self.start()
            cfg = self.config()
            report.paused = self.paused()
            for step in (
                self._housekeeping,
                self._scan_external,
                self._promote_blocked,
                self._retry_failed,
            ):
                try:
                    step(cfg, report)
                except Exception as exc:  # one failing step must not stop the others
                    log.exception("scheduler step %s failed", step.__name__)
                    report.errors.append(f"{step.__name__}: {redact(str(exc))[:200]}")
            if not report.paused:
                try:
                    self._dispatch(cfg, report)
                except Exception as exc:
                    log.exception("scheduler dispatch failed")
                    report.errors.append(f"dispatch: {redact(str(exc))[:200]}")
            self._last = report
            return report
        finally:
            self._tick_lock.release()

    # ----------------------------------------------------------------- housekeeping
    def _in_flight(self) -> list[tuple[int, int]]:
        """(task id, project id) of RESERVED/RUNNING tasks that really have a worker/launch."""
        with Session(self.engine) as s:
            rows = s.execute(
                select(Task.id, Task.project_id)
                .where(Task.status.in_(("RESERVED", "RUNNING")))
                .order_by(Task.id)
            ).all()
        return [(t, p) for t, p in rows if self.supervisor.busy(t)]

    def _housekeeping(self, _cfg: SchedulerConfig, report: TickReport) -> None:
        with Session(self.engine) as s:
            live = {
                tid: st
                for tid, st in s.execute(
                    select(Task.id, Task.status).where(Task.status.in_(("RESERVED", "RUNNING")))
                )
            }
            holders = set(s.scalars(select(Reservation.task_id).distinct()))
        for tid, st in sorted(live.items()):
            if self.supervisor.busy(tid):
                self.reservations.heartbeat(tid)
            elif st == "RESERVED":  # reserved, but the launch never happened (crash, refusal)
                try:
                    self.tasks.transition(
                        tid, "READY", actor=ACTOR, reason="reserved without a worker"
                    )
                    report.demoted.append(tid)
                except IllegalTransition:
                    continue
                self.admission.release(tid, reason="no worker for reserved task")
            # RUNNING without a worker is an orphan: restart reconciliation (WP9) owns it.
        for tid in sorted(holders - set(live)):
            if self.admission.release(tid, reason="task left RESERVED/RUNNING"):
                report.released.append(tid)

    def _scan_external(self, cfg: SchedulerConfig, report: TickReport) -> None:
        now = self.monotonic()
        for pid in sorted({p for _t, p in self._in_flight()}):
            if now - self._last_scan.get(pid, -1e18) < cfg.external_scan_s:
                continue
            self._last_scan[pid] = now
            try:
                self.watcher.scan(pid)
            except Exception as exc:  # git missing, project moved: report, keep scheduling
                report.errors.append(f"external scan of project {pid}: {redact(str(exc))[:160]}")

    def _promote_blocked(self, _cfg: SchedulerConfig, report: TickReport) -> None:
        with Session(self.engine) as s:
            blocked = s.execute(
                select(Task.id, Task.block_reason)
                .where(Task.status == "BLOCKED", Task.id.in_(select(TaskDep.task_id)))
                .order_by(Task.id)
            ).all()
            ready = []
            for tid, why in blocked:
                if not (why or "").startswith(_WAITING):
                    continue  # blocked by hand or by a cancelled dependency: a person decides
                dep_status = s.scalars(
                    select(Task.status)
                    .join(TaskDep, TaskDep.depends_on_id == Task.id)
                    .where(TaskDep.task_id == tid)
                )
                if all(d in states.DONE for d in dep_status):
                    ready.append(tid)
        for tid in ready:
            try:
                self.tasks.transition(tid, "READY", actor=ACTOR, reason="dependencies done")
                report.promoted.append(tid)
            except IllegalTransition:
                continue

    def _retry_failed(self, cfg: SchedulerConfig, report: TickReport) -> None:
        now = self.clock()
        with Session(self.engine) as s:
            failed = s.execute(
                select(Task.id, Task.project_id, Task.retry_count, Task.billing_mode)
                .where(Task.status == "FAILED")
                .order_by(Task.id)
            ).all()
            plan = []
            for tid, pid, retries, billing in failed:
                rows = s.scalars(
                    select(Run)
                    .where(Run.task_id == tid, Run.ended_at.is_not(None))
                    .order_by(Run.id)
                ).all()
                plan.append(
                    (
                        tid,
                        pid,
                        retries,
                        billing,
                        [
                            FailedRun(r.status, _reason(r.result_json), _aware(r.ended_at))
                            for r in rows
                        ],
                    )  # fmt: skip
                )
        for tid, pid, retries, billing, runs in plan:
            d = policy.retry_decision(runs, retries, billing, cfg, now)
            stub = _Ref(tid, pid)
            if d.action == "wait":
                continue
            if d.action == "give_up":
                if self._event_once(stub, "scheduler.gave_up", why=d.why, retries=retries):
                    report.gave_up.append(tid)
                continue
            try:
                self.tasks.transition(tid, "READY", actor=ACTOR, reason=f"auto retry: {d.why}")
            except IllegalTransition:
                continue
            self._event_once(stub, "scheduler.retry", why=d.why, retries=retries + 1)
            report.retried.append(tid)

    # ----------------------------------------------------------------- dispatch
    def _heavy_holders(self) -> set[int]:
        with Session(self.engine) as s:
            return set(
                s.scalars(
                    select(Reservation.task_id).where(
                        Reservation.resource_type == HEAVY_CLASS[0],
                        Reservation.resource_key == HEAVY_CLASS[1],
                    )
                )
            )

    def _candidates(self) -> tuple[list[Candidate], dict[int, TaskData], dict[int, list[Any]]]:
        queue = self.tasks.ready_queue()
        by_id = {t.id: t for t in queue}
        extras: dict[int, list[Any]] = {}
        heavy_by_project: dict[int, frozenset[str]] = {}
        out: list[Candidate] = []
        with Session(self.engine) as s:
            for t in queue:
                if t.project_id not in heavy_by_project:
                    proj = s.get(Project, t.project_id)
                    prof = profile_for(proj.profile) if proj else None
                    heavy_by_project[t.project_id] = self.admission.heavy_types(prof)
                extra = list(self.resources_for(t))
                extras[t.id] = extra
                heavy = any(r.type in heavy_by_project[t.project_id] for r in extra)
                out.append(Candidate(t.id, t.project_id, t.priority, t.estimate_microusd, heavy))
        out.sort(key=policy.order_key)
        return out, by_id, extras

    def _dispatch(self, cfg: SchedulerConfig, report: TickReport) -> None:
        cands, by_id, extras = self._candidates()
        if not cands:
            return
        flight = self._in_flight()
        total = len(flight)
        per_project: dict[int, int] = {}
        for _t, p in flight:
            per_project[p] = per_project.get(p, 0) + 1
        heavy_running = len(self._heavy_holders())
        limit = self.global_limit()
        for c in cands:
            if total >= limit:
                report.skipped[c.task_id] = f"waiting: {limit} workers already running"
                continue
            if cfg.per_project_limit and per_project.get(c.project_id, 0) >= cfg.per_project_limit:
                report.skipped[c.task_id] = (
                    f"waiting: project already runs {cfg.per_project_limit} worker(s)"
                )
                continue
            if c.heavy and heavy_running >= cfg.heavy_limit:
                report.skipped[c.task_id] = "waiting: another heavy task is running"
                continue
            cool = self._cooldown.get(c.task_id)
            if cool and self.monotonic() < cool[0]:
                report.skipped[c.task_id] = "waiting: backing off after a refused start"
                continue
            if self._launch(by_id[c.task_id], extras[c.task_id], cfg, report):
                total += 1
                per_project[c.project_id] = per_project.get(c.project_id, 0) + 1
                heavy_running += int(c.heavy)

    def _launch(
        self, task: TaskData, extra: list[Any], cfg: SchedulerConfig, report: TickReport
    ) -> bool:
        started = False
        try:
            try:
                self.admission.reserve(task.id, extra=extra)
            except ReservationConflict as exc:
                report.skipped[task.id] = "blocked by: " + (
                    "; ".join(b.message for b in exc.blockers) or "conflict"
                )
                return False
            except (IllegalTransition, TaskError) as exc:  # lost a race (cancel, edit)
                report.skipped[task.id] = f"not startable: {exc}"
                return False
            try:
                self.supervisor.start(task.id, **self._start_args(task, cfg))
            except RunError as exc:
                if "already has a running worker" in str(exc) or "maximum number" in str(exc):
                    report.skipped[task.id] = f"waiting: {exc}"
                else:
                    self._defer(task, exc, report)
                return False
            except Exception as exc:  # worktree/git problem etc.: defer, never crash the loop
                log.exception("start of task %s failed", task.id)
                self._defer(task, exc, report)
                return False
            started = True
            self._cooldown.pop(task.id, None)
            report.started.append(task.id)
            return True
        finally:
            if not started:
                self._unwind(task.id)

    def _unwind(self, task_id: int) -> None:
        """A launch that did not happen: free the reservations, put a RESERVED task back."""
        if self.supervisor.busy(task_id):
            return
        try:
            self.tasks.transition(task_id, "READY", actor=ACTOR, reason="launch did not happen")
        except (IllegalTransition, TaskError):
            pass  # was never RESERVED, or a person moved it meanwhile
        self.admission.release(task_id, reason="launch did not happen")

    def _start_args(self, task: TaskData, cfg: SchedulerConfig) -> dict[str, Any]:
        args: dict[str, Any] = {
            "hang_timeout_s": cfg.hang_timeout_s, "max_runtime_s": cfg.max_runtime_s,
        }  # fmt: skip
        if task.billing_mode == "api":
            # The per-run cap an API task asks for is its own estimate, bounded by the live-run
            # maximum; the supervisor and the budget ledger refuse anything above their ceilings.
            ceiling = self.supervisor.settings.live_run_max_microusd
            args["max_budget_microusd"] = (
                min(task.estimate_microusd, ceiling) if task.estimate_microusd > 0 else ceiling
            )
        args.update(self.start_args_for(task))
        return args

    def _defer(self, task: TaskData, exc: BaseException, report: TickReport) -> None:
        reason = redact(str(exc))[:300] or exc.__class__.__name__
        until, n = self._cooldown.get(task.id, (0.0, 0))
        delay = COOLDOWN_STEPS_S[min(n, len(COOLDOWN_STEPS_S) - 1)]
        self._cooldown[task.id] = (self.monotonic() + delay, n + 1)
        report.skipped[task.id] = f"deferred: {reason}"
        self._event_once(task, "scheduler.deferred", reason=reason)


def _reason(result_json: str | None) -> str | None:
    if not result_json:
        return None
    try:
        data = json.loads(result_json)
    except ValueError:
        return None
    return str(data.get("reason")) if isinstance(data, dict) else None


async def run_forever(scheduler: Scheduler, interval_s: float = 2.0) -> None:
    """Asyncio driver: one tick at a time in a worker thread (SQLite and git calls block)."""
    import asyncio

    while True:
        try:
            await asyncio.to_thread(scheduler.tick)
        except Exception:  # tick already isolates its steps; this is the last guard
            log.exception("scheduler tick crashed")
        await asyncio.sleep(interval_s)
