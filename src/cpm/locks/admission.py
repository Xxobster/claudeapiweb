"""Conflict check and reservation for one task: the "reserve" step of the scheduler (WP7).

Order (ARCHITECTURE section 3, rule 2): declared scope + lane resources -> external locks
(lock files, job board) -> CPM reservations and Desktop claims (atomic) -> READY -> RESERVED.
Budget admission (WP8) comes after this and the worker is started only after both.
A failed step leaves the task READY with a visible reason (a `reservation.blocked` event; one per
distinct reason, so a scheduler tick does not flood the log). Call `release` in a `finally`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from sqlalchemy import Engine, insert, select
from sqlalchemy.orm import Session

from cpm.db.models import Event, Project, Task, utcnow
from cpm.locks import normalize
from cpm.locks.adapters import ExternalLock
from cpm.locks.adapters.devlock import DevLock
from cpm.locks.adapters.jobboard import JobBoard
from cpm.locks.adapters.lockfiles import LockDir, LockFile
from cpm.locks.reservations import ReservationService
from cpm.locks.types import (
    HEAVY_DEFAULT,
    Blocker,
    Grant,
    ReservationConflict,
    ResourceRequest,
)
from cpm.profiles import Profile, ProfileError, load_profile
from cpm.security.paths import PathPolicyError
from cpm.security.redaction import redact
from cpm.tasks.service import IllegalTransition, TaskData, TaskError, TaskService

ACTOR = "scheduler"


def profile_for(name: str | None) -> Profile | None:
    if not name:
        return None
    try:
        return load_profile(name)
    except ProfileError:
        return None


def adapters_for(project: Project, profile: Profile | None) -> list[ExternalLock]:
    """External lock adapters configured by the project's profile (none without a profile)."""
    out: list[ExternalLock] = []
    if profile is None:
        return out
    for name, path in profile.lock_files.items():
        out.append(LockFile(name, path))
    for name, path in profile.lock_dirs.items():
        out.append(LockDir(name, path))
    board = profile.external_locks.get("job_board")
    if board:
        out.append(JobBoard(project.path, board))
    out.append(DevLock())
    return out


class Admission:
    def __init__(self, engine: Engine, reservations: ReservationService) -> None:
        self.engine = engine
        self.reservations = reservations
        self.tasks = TaskService(engine)

    # ----------------------------------------------------------------- inputs
    def _load(self, task_id: int) -> tuple[Task, Project]:
        with Session(self.engine) as s:
            t = s.get(Task, task_id)
            if t is None:
                raise TaskError(f"task {task_id} not found")
            p = s.get(Project, t.project_id)
            assert p is not None  # FK  # noqa: S101
            s.expunge_all()
            return t, p

    def requests_for(
        self, task: Task, project: Project, extra: Sequence[ResourceRequest] = ()
    ) -> list[ResourceRequest]:
        """Declared paths (exclusive) normalised against the project, plus lane resources."""
        reqs: list[ResourceRequest] = []
        for raw in json.loads(task.declared_scope):
            key, label = normalize.normalize_scope(project.path, raw)
            reqs.append(ResourceRequest("path", key, "excl", label))
        reqs.extend(extra)
        return reqs

    def heavy_types(self, profile: Profile | None) -> frozenset[str]:
        return (
            frozenset(profile.heavy_resources)
            if profile and profile.heavy_resources
            else HEAVY_DEFAULT
        )

    # ----------------------------------------------------------------- checks
    def evaluate(self, task_id: int, extra: Sequence[ResourceRequest] = ()) -> list[Blocker]:
        """Everything that would stop this task now (external locks + reservations + claims)."""
        task, project = self._load(task_id)
        profile = profile_for(project.profile)
        try:
            reqs = self.requests_for(task, project, extra)
        except PathPolicyError as exc:
            return [Blocker("scope", "declared_scope", f"blocked by: invalid scope ({exc})")]
        return self._blockers(task, project, profile, reqs)

    def _blockers(
        self, task: Task, project: Project, profile: Profile | None, reqs: list[ResourceRequest]
    ) -> list[Blocker]:
        heavy_types = self.heavy_types(profile)
        heavy = any(r.type in heavy_types for r in reqs)
        out: list[Blocker] = []
        for adapter in adapters_for(project, profile):
            out.extend(adapter.blockers(reqs, heavy=heavy))
        out.extend(self.reservations.check(task.id, project.id, reqs, heavy_types=heavy_types))
        return out

    # ----------------------------------------------------------------- reserve / release
    def _record_blocked(self, task: Task, blockers: list[Blocker]) -> None:
        messages = [b.message for b in blockers]
        with Session(self.engine) as s:
            last = s.scalars(
                select(Event)
                .where(
                    Event.task_id == task.id,
                    Event.kind.in_(("reservation.blocked", "reservation.granted")),
                )
                .order_by(Event.id.desc())
                .limit(1)
            ).first()
            if last is not None and last.kind == "reservation.blocked":
                if json.loads(last.detail).get("blockers") == messages:
                    return
            s.execute(
                insert(Event).values(
                    ts=utcnow(), project_id=task.project_id, task_id=task.id,
                    kind="reservation.blocked",
                    detail=redact(json.dumps({"blockers": messages})),
                )
            )  # fmt: skip
            s.commit()

    def reserve(
        self,
        task_id: int,
        extra: Sequence[ResourceRequest] = (),
        *,
        lease_seconds: int | None = None,
        on_grant: Callable[[Grant], None] | None = None,
    ) -> TaskData:
        """READY -> RESERVED with the full set, or `ReservationConflict` (the task stays READY)."""
        task, project = self._load(task_id)
        if task.status != "READY":
            raise IllegalTransition(f"only READY tasks can be reserved (is {task.status})")
        profile = profile_for(project.profile)
        try:
            reqs = self.requests_for(task, project, extra)
        except PathPolicyError as exc:
            blockers = [Blocker("scope", "declared_scope", f"blocked by: invalid scope ({exc})")]
            self._record_blocked(task, blockers)
            raise ReservationConflict(blockers) from exc
        heavy_types = self.heavy_types(profile)
        heavy = any(r.type in heavy_types for r in reqs)
        external: list[Blocker] = []
        for adapter in adapters_for(project, profile):
            external.extend(adapter.blockers(reqs, heavy=heavy))
        if external:
            self._record_blocked(task, external)
            raise ReservationConflict(external)
        try:
            grant = self.reservations.acquire(
                task.id, project.id, reqs, lease_seconds=lease_seconds, heavy_types=heavy_types
            )
        except ReservationConflict as exc:
            self._record_blocked(task, exc.blockers)
            raise
        try:
            if on_grant:
                on_grant(grant)
            return self.tasks.transition(task.id, "RESERVED", actor=ACTOR)
        except BaseException:
            self.reservations.release(task.id, reason="reserve failed")
            raise

    def release(self, task_id: int, *, reason: str = "released") -> int:
        return self.reservations.release(task_id, reason=reason)

    def last_blockers(self, task_id: int) -> list[str]:
        """The reason the task is still waiting, for the UI ("blocked by: ...")."""
        with Session(self.engine) as s:
            last = s.scalars(
                select(Event)
                .where(
                    Event.task_id == task_id,
                    Event.kind.in_(("reservation.blocked", "reservation.granted")),
                )
                .order_by(Event.id.desc())
                .limit(1)
            ).first()
        if last is None or last.kind != "reservation.blocked":
            return []
        return [str(m) for m in json.loads(last.detail).get("blockers", [])]
