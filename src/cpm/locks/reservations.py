"""Reservations: atomic multi-resource acquisition with prefix semantics and leases (WP6).

* A task's whole set is granted in ONE `BEGIN IMMEDIATE` transaction or not at all.
* Paths use prefix semantics (`normalize.overlaps`); shared/shared is compatible, everything else
  that overlaps conflicts. Named resources (`unity_editor`, `gpu`, ...) conflict on the same key.
* Any request of a heavy type also takes the implicit class resource `class:heavy` exclusively,
  so at most one heavy task holds resources at a time (DECISIONS Q8: max 1 heavy task).
* Leases: a holder renews with `heartbeat`. A lapsed lease is removed only after the liveness
  check says the holder is gone; a live holder just gets a fresh lease (PLAN.md section 3).
* Desktop claims overlapping an exclusive path request block it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, delete, insert, select, update

from cpm.db.models import DesktopClaim, Event, Reservation, utcnow
from cpm.locks import normalize
from cpm.locks.txn import immediate
from cpm.locks.types import (
    HEAVY_DEFAULT,
    MAX_REQUESTS,
    Blocker,
    Grant,
    ReservationConflict,
    ReservationInfo,
    ResourceRequest,
)
from cpm.security.redaction import redact

DEFAULT_LEASE_S = 120
MAX_LEASE_S = 3600
_HEAVY_REQ = ResourceRequest(type=normalize.HEAVY_CLASS[0], key=normalize.HEAVY_CLASS[1])


def _compatible(a_mode: str, b_mode: str) -> bool:
    return a_mode == "shared" and b_mode == "shared"


def _clash(req: ResourceRequest, req_project: int | None, row: Any) -> bool:
    if req.type != row.resource_type:
        return False
    if req.per_project and req_project != row.project_id:
        return False
    if _compatible(req.mode, row.mode):
        return False
    if req.type == "path":
        return bool(normalize.overlaps(req.key, row.resource_key))
    return bool(req.key == row.resource_key)


def _describe(row: Any) -> str:
    what = row.label or row.resource_key
    return f"task #{row.task_id} holds {row.resource_type} `{what}`"


class ReservationService:
    def __init__(
        self,
        engine: Engine,
        *,
        liveness: Callable[[int], bool] | None = None,
        heavy_types: frozenset[str] = HEAVY_DEFAULT,
        lease_seconds: int = DEFAULT_LEASE_S,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.engine = engine
        self.liveness = liveness or (lambda _task_id: False)
        self.heavy_types = heavy_types
        self.lease_seconds = lease_seconds
        self.clock = clock

    # ------------------------------------------------------------------ helpers
    def _lease(self, lease_seconds: int | None) -> int:
        n = self.lease_seconds if lease_seconds is None else lease_seconds
        return max(1, min(int(n), MAX_LEASE_S))

    def _with_heavy(
        self, requests: Sequence[ResourceRequest], heavy_types: frozenset[str] | None
    ) -> list[ResourceRequest]:
        if len(requests) > MAX_REQUESTS:
            raise ValueError(f"more than {MAX_REQUESTS} resources requested")
        heavy = heavy_types if heavy_types is not None else self.heavy_types
        # Coalesce by canonical identity: `Hero.prefab` and `Hero.prefab.meta`, case variants and
        # symlink aliases are one resource (the stricter mode wins, the first label is kept).
        merged: dict[tuple[str, str], ResourceRequest] = {}
        for r in requests:
            ident = (r.type, r.key)
            prev = merged.get(ident)
            if prev is None:
                merged[ident] = r
            elif prev.mode == "shared" and r.mode == "excl":
                merged[ident] = ResourceRequest(r.type, r.key, "excl", prev.label)
        out = list(merged.values())
        if any(r.type in heavy for r in out) and _HEAVY_REQ not in out:
            out.append(_HEAVY_REQ)
        return out

    def _log(
        self,
        conn: Connection,
        task_id: int | None,
        project_id: int | None,
        kind: str,
        **detail: Any,
    ) -> None:
        conn.execute(
            insert(Event).values(
                ts=self.clock(), project_id=project_id, task_id=task_id, kind=kind,
                detail=redact(json.dumps(detail, default=str)),
            )
        )  # fmt: skip

    def _liveness_snapshot(self, now: datetime) -> dict[int, bool]:
        """Liveness of every holder with a lapsed lease, asked BEFORE any write lock is taken.

        The liveness callback may take the supervisor's lock; calling it while holding SQLite's
        write lock could deadlock against a supervisor thread that writes under its own lock.
        """
        with self.engine.connect() as conn:
            lapsed = [
                t
                for (t,) in conn.execute(
                    select(Reservation.task_id)
                    .where(Reservation.lease_expires_at <= now)
                    .distinct()
                )
            ]
        return {t: bool(self.liveness(t)) for t in lapsed}

    def _expire(self, conn: Connection, now: datetime, alive: dict[int, bool]) -> None:
        """Drop lapsed leases whose holder is gone; renew those whose holder is alive.

        A holder whose lease lapsed after the snapshot was taken counts as alive (renewed): the
        safe error is keeping a lease one round longer, never granting a live holder's resource.
        """
        lapsed = conn.execute(
            select(Reservation.task_id, Reservation.project_id)
            .where(Reservation.lease_expires_at <= now)
            .distinct()
        ).all()
        for task_id, project_id in lapsed:
            if alive.get(task_id, True):
                conn.execute(
                    update(Reservation)
                    .where(Reservation.task_id == task_id)
                    .values(
                        heartbeat_at=now,
                        lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                    )
                )
            else:
                conn.execute(delete(Reservation).where(Reservation.task_id == task_id))
                self._log(conn, task_id, project_id, "reservation.expired")

    def _load(
        self,
        conn: Connection,
        task_id: int,
        project_id: int | None,
        now: datetime,
        alive: dict[int, bool],
    ) -> tuple[list[Any], list[Any]]:
        """Other tasks' effective reservations and the project's open Desktop claims."""
        rows = [
            r for r in conn.execute(select(Reservation).where(Reservation.task_id != task_id)).all()
        ]
        lapsed = {
            tid
            for (tid,) in conn.execute(
                select(Reservation.task_id).where(Reservation.lease_expires_at <= now).distinct()
            )
        }
        # A lapsed lease only counts while its holder is still alive (it is renewed on write).
        dead = {t for t in lapsed if not alive.get(t, True)}
        rows = [r for r in rows if r.task_id not in dead]
        claims = conn.execute(
            select(DesktopClaim).where(
                DesktopClaim.project_id == project_id,
                DesktopClaim.released_at.is_(None),
                (DesktopClaim.expires_at.is_(None)) | (DesktopClaim.expires_at > now),
            )
        ).all()
        return rows, list(claims)

    @staticmethod
    def _blockers_from(
        rows: list[Any],
        claims: list[Any],
        project_id: int | None,
        reqs: Sequence[ResourceRequest],
    ) -> list[Blocker]:
        blockers: list[Blocker] = []
        for req in reqs:
            for row in rows:
                if _clash(req, project_id, row):
                    kind = "heavy" if req.type == "class" else "reservation"
                    blockers.append(
                        Blocker(
                            kind, req.label or req.key,
                            f"blocked by: {_describe(row)} (needed `{req.label or req.key}`)",
                            row.task_id,
                        )
                    )  # fmt: skip
            if req.type == "path" and req.mode == "excl":
                for c in claims:
                    if normalize.overlaps(req.key, c.path_key):
                        blockers.append(
                            Blocker(
                                "desktop_claim", req.label or req.key,
                                f"blocked by: Desktop claim on `{c.label}`"
                                + (f" ({c.note})" if c.note else ""),
                            )
                        )  # fmt: skip
        return blockers

    # ------------------------------------------------------------------ public API
    def check(
        self,
        task_id: int,
        project_id: int | None,
        requests: Sequence[ResourceRequest],
        *,
        heavy_types: frozenset[str] | None = None,
    ) -> list[Blocker]:
        """Dry run: why would `acquire` fail right now? Writes nothing."""
        reqs = self._with_heavy(requests, heavy_types)
        now = self.clock()
        alive = self._liveness_snapshot(now)
        with self.engine.connect() as conn:
            rows, claims = self._load(conn, task_id, project_id, now, alive)
        return self._blockers_from(rows, claims, project_id, reqs)

    def acquire(
        self,
        task_id: int,
        project_id: int | None,
        requests: Sequence[ResourceRequest],
        *,
        lease_seconds: int | None = None,
        heavy_types: frozenset[str] | None = None,
    ) -> Grant:
        """Grant the whole set atomically or raise `ReservationConflict` (nothing reserved).

        Re-acquiring replaces the task's previous set, so it is safe to call again after the
        scope changed. The task's own rows never conflict with themselves.
        """
        reqs = self._with_heavy(requests, heavy_types)
        lease = self._lease(lease_seconds)
        alive = self._liveness_snapshot(self.clock())
        with immediate(self.engine) as conn:
            now = self.clock()
            self._expire(conn, now, alive)
            rows, claims = self._load(conn, task_id, project_id, now, alive)
            blockers = self._blockers_from(rows, claims, project_id, reqs)
            if blockers:
                raise ReservationConflict(blockers)
            conn.execute(delete(Reservation).where(Reservation.task_id == task_id))
            expires = now + timedelta(seconds=lease)
            for r in reqs:
                conn.execute(
                    insert(Reservation).values(
                        resource_type=r.type, resource_key=r.key, task_id=task_id,
                        mode=r.mode, project_id=project_id, label=r.label or r.key,
                        lease_expires_at=expires, heartbeat_at=now,
                    )
                )  # fmt: skip
            self._log(
                conn, task_id, project_id, "reservation.granted",
                resources=[f"{r.type}:{r.label or r.key}" for r in reqs], lease_seconds=lease,
            )  # fmt: skip
        return Grant(task_id=task_id, resources=reqs, lease_seconds=lease)

    def heartbeat(self, task_id: int, *, lease_seconds: int | None = None) -> int:
        """Renew the task's lease. Returns the number of rows renewed (0 = nothing held)."""
        lease = self._lease(lease_seconds)
        with immediate(self.engine) as conn:
            now = self.clock()
            res = conn.execute(
                update(Reservation)
                .where(Reservation.task_id == task_id)
                .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease))
            )
            return int(res.rowcount)

    def release(self, task_id: int, *, reason: str = "released") -> int:
        """Release everything the task holds. Idempotent, so it is safe in a `finally`."""
        with immediate(self.engine) as conn:
            pid = conn.execute(
                select(Reservation.project_id).where(Reservation.task_id == task_id).limit(1)
            ).scalar()
            res = conn.execute(delete(Reservation).where(Reservation.task_id == task_id))
            n = int(res.rowcount)
            if n:
                self._log(conn, task_id, pid, "reservation.released", reason=reason, count=n)
            return n

    def sweep(self) -> None:
        """Expire lapsed leases whose holder is gone (called by the scheduler / recovery)."""
        alive = self._liveness_snapshot(self.clock())
        with immediate(self.engine) as conn:
            self._expire(conn, self.clock(), alive)

    def active(
        self, project_id: int | None = None, task_id: int | None = None
    ) -> list[ReservationInfo]:
        q = select(Reservation).order_by(Reservation.id)
        if project_id is not None:
            q = q.where(Reservation.project_id == project_id)
        if task_id is not None:
            q = q.where(Reservation.task_id == task_id)
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        return [
            ReservationInfo(
                r.id, r.task_id, r.project_id, r.resource_type, r.resource_key, r.label, r.mode,
                r.heartbeat_at.isoformat(), r.lease_expires_at.isoformat(),
            )
            for r in rows
        ]  # fmt: skip
