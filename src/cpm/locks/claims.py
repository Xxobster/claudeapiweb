"""Desktop claims: "Claude Desktop is working on these paths" (manual, optional expiry).

A claim blocks every exclusive path reservation that overlaps it (see `ReservationService`).
Adding a claim never stops a running worker: the claim is returned together with the tasks that
already hold overlapping paths, so the UI can warn and the user can pause them. Claims live in
CPM's database only; the job board in a managed repo is never written (WP11, rule 40).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import Engine, insert, select, update

from cpm.db.models import DesktopClaim, Event, Project, Reservation, utcnow
from cpm.locks import normalize
from cpm.locks.txn import immediate
from cpm.security.paths import PathPolicyError
from cpm.security.redaction import redact

MAX_CLAIMS = 200
MAX_EXPIRY_S = 30 * 24 * 3600


class ClaimError(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ClaimInfo:
    id: int
    project_id: int
    path: str
    note: str
    created_at: str
    expires_at: str | None
    released_at: str | None
    affected_tasks: list[int]


def _iso(v: object) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else None


class DesktopClaims:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _info(self, c: Any, affected: list[int]) -> ClaimInfo:
        return ClaimInfo(
            c.id, c.project_id, c.label, c.note,
            _iso(c.created_at) or "", _iso(c.expires_at), _iso(c.released_at), affected,
        )  # fmt: skip

    def add(
        self,
        project_id: int,
        path: str,
        *,
        note: str = "",
        expires_in_s: int | None = None,
        actor: str = "user",
    ) -> ClaimInfo:
        if expires_in_s is not None and not 1 <= expires_in_s <= MAX_EXPIRY_S:
            raise ClaimError("expires_in_s out of range")
        with self.engine.connect() as conn:
            root = conn.execute(select(Project.path).where(Project.id == project_id)).scalar()
        if root is None:
            raise ClaimError("unknown project", 404)
        try:
            key, label = normalize.normalize_scope(root, path)
        except PathPolicyError as exc:
            raise ClaimError(str(exc)) from exc
        note = redact(note.strip())[:300]
        with immediate(self.engine) as conn:
            now = utcnow()
            expires = None if expires_in_s is None else now + timedelta(seconds=expires_in_s)
            open_claims = conn.execute(
                select(DesktopClaim.id).where(
                    DesktopClaim.project_id == project_id, DesktopClaim.released_at.is_(None)
                )
            ).all()
            if len(open_claims) >= MAX_CLAIMS:
                raise ClaimError(f"more than {MAX_CLAIMS} open claims", 409)
            ins = conn.execute(
                insert(DesktopClaim).values(
                    project_id=project_id, path_key=key, label=label, note=note, created_at=now,
                    expires_at=expires,
                )
            )  # fmt: skip
            cid = int((ins.inserted_primary_key or (0,))[0])
            affected = sorted(
                {
                    r.task_id
                    for r in conn.execute(
                        select(Reservation).where(
                            Reservation.project_id == project_id,
                            Reservation.resource_type == "path",
                        )
                    )
                    if normalize.overlaps(key, r.resource_key)
                }
            )
            conn.execute(
                insert(Event).values(
                    ts=now, project_id=project_id, kind="desktop_claim.added",
                    detail=redact(
                        json.dumps(
                            {"claim": cid, "path": label, "actor": actor, "affected": affected}
                        )
                    ),
                )
            )  # fmt: skip
        return self.get(cid, affected)

    def get(self, claim_id: int, affected: list[int] | None = None) -> ClaimInfo:
        with self.engine.connect() as conn:
            c = conn.execute(select(DesktopClaim).where(DesktopClaim.id == claim_id)).first()
        if c is None:
            raise ClaimError("unknown claim", 404)
        return self._info(c, affected or [])

    def list(self, project_id: int, *, include_inactive: bool = False) -> list[ClaimInfo]:
        now = utcnow()
        q = select(DesktopClaim).where(DesktopClaim.project_id == project_id)
        if not include_inactive:
            q = q.where(
                DesktopClaim.released_at.is_(None),
                (DesktopClaim.expires_at.is_(None)) | (DesktopClaim.expires_at > now),
            )
        with self.engine.connect() as conn:
            rows = conn.execute(q.order_by(DesktopClaim.id)).all()
        return [self._info(r, []) for r in rows]

    def release(self, claim_id: int, *, actor: str = "user") -> ClaimInfo:
        with immediate(self.engine) as conn:
            c = conn.execute(select(DesktopClaim).where(DesktopClaim.id == claim_id)).first()
            if c is None:
                raise ClaimError("unknown claim", 404)
            if c.released_at is None:
                now = utcnow()
                conn.execute(
                    update(DesktopClaim).where(DesktopClaim.id == claim_id).values(released_at=now)
                )
                conn.execute(
                    insert(Event).values(
                        ts=now, project_id=c.project_id, kind="desktop_claim.released",
                        detail=json.dumps(
                            {"claim": claim_id, "path": redact(c.label), "actor": actor}
                        ),
                    )
                )  # fmt: skip
        return self.get(claim_id)
