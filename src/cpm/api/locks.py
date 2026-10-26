"""Reservations, Desktop claims, external locks and external-change scan (WP6).

Nothing here grants a reservation: that is the scheduler's job (`locks.admission.Admission`),
so a state-machine step cannot be skipped over HTTP. These routes explain, claim and release.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from cpm.db.models import Project, Task
from cpm.locks.admission import Admission, adapters_for, profile_for
from cpm.locks.boardpr import JobBoardService
from cpm.locks.claims import ClaimError, DesktopClaims
from cpm.locks.external import ExternalWatcher
from cpm.tasks.service import TaskError

router = APIRouter(prefix="/api", tags=["locks"])


class ClaimIn(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    note: str = Field(default="", max_length=300)
    expires_in_s: int | None = Field(default=None, ge=1, le=30 * 24 * 3600)


def _project(request: Request, project_id: int) -> Project:
    with Session(request.app.state.engine) as s:
        p = s.get(Project, project_id)
        if p is None:
            raise HTTPException(404, "unknown project")
        s.expunge(p)
        return p


def _claims(request: Request) -> DesktopClaims:
    return DesktopClaims(request.app.state.engine)


def _call(fn: Any, *a: Any, **kw: Any) -> Any:
    try:
        return fn(*a, **kw)
    except ClaimError as e:
        raise HTTPException(e.status, str(e)) from e


@router.get("/projects/{project_id}/reservations")
def list_reservations(project_id: int, request: Request) -> dict[str, Any]:
    _project(request, project_id)
    rows = request.app.state.reservations.active(project_id=project_id)
    return {"reservations": [asdict(r) for r in rows]}


@router.get("/tasks/{task_id}/conflicts")
def task_conflicts(task_id: int, request: Request) -> dict[str, Any]:
    """Why this task cannot start now ("blocked by: Desktop claim on `Assets/Game/UI/`")."""
    adm: Admission = request.app.state.admission
    with Session(request.app.state.engine) as s:
        if s.get(Task, task_id) is None:
            raise HTTPException(404, "unknown task")
    blockers = adm.evaluate(task_id)
    return {
        "blockers": [b.as_dict() for b in blockers],
        "last_recorded": adm.last_blockers(task_id),
    }


@router.post("/tasks/{task_id}/reservations/release")
def release_task(task_id: int, request: Request) -> dict[str, Any]:
    """Manual release of a stuck reservation. Refused while a worker is alive for the task."""
    with Session(request.app.state.engine) as s:
        if s.get(Task, task_id) is None:
            raise HTTPException(404, "unknown task")
    if request.app.state.supervisor.live_run_for_task(task_id) is not None:
        raise HTTPException(409, "a worker is running for this task; stop it first")
    n = request.app.state.reservations.release(task_id, reason="released by user")
    return {"released": n}


@router.get("/projects/{project_id}/desktop-claims")
def list_claims(
    project_id: int, request: Request, include_inactive: bool = False
) -> dict[str, Any]:
    _project(request, project_id)
    return {
        "claims": [
            asdict(c) for c in _claims(request).list(project_id, include_inactive=include_inactive)
        ]
    }


@router.post("/projects/{project_id}/desktop-claims", status_code=201)
def add_claim(project_id: int, body: ClaimIn, request: Request) -> dict[str, Any]:
    _project(request, project_id)
    claim = _call(
        _claims(request).add, project_id, body.path, note=body.note, expires_in_s=body.expires_in_s
    )
    return asdict(claim)


@router.delete("/desktop-claims/{claim_id}")
def release_claim(claim_id: int, request: Request) -> dict[str, Any]:
    return asdict(_call(_claims(request).release, claim_id))


@router.get("/projects/{project_id}/locks")
def external_locks(project_id: int, request: Request) -> dict[str, Any]:
    """State of the managed project's own locks (read-only)."""
    p = _project(request, project_id)
    return {"locks": [a.status() for a in adapters_for(p, profile_for(p.profile))]}


@router.post("/projects/{project_id}/external-scan")
def external_scan(project_id: int, request: Request) -> dict[str, Any]:
    """Compare the main tree with the last snapshot; report edits that hit reserved paths."""
    _project(request, project_id)
    try:
        res = ExternalWatcher(request.app.state.engine).scan(project_id)
    except Exception as exc:  # git missing/timeout: report, never crash the server
        raise HTTPException(
            502, f"main tree could not be inspected ({type(exc).__name__})"
        ) from exc
    return {
        "baseline": res.baseline,
        "changed": res.changed[:200],
        "truncated": res.truncated,
        "conflicts": [asdict(c) for c in res.conflicts],
    }


# ------------------------------------------------------------------ job board PRs (WP11)
def _board(request: Request) -> JobBoardService:
    return request.app.state.jobboard  # type: ignore[no-any-return]


def _board_call(fn: Any, *a: Any) -> Any:
    try:
        return fn(*a)
    except TaskError as e:  # BoardPrError, NotFound
        raise HTTPException(e.status, str(e)) from e


@router.get("/tasks/{task_id}/job-board")
def job_board_status(task_id: int, request: Request) -> dict[str, Any]:
    """CPM's claim / release pull requests for the task and what the base branch's board shows."""
    return _board_call(_board(request).status, task_id)  # type: ignore[no-any-return]


@router.post("/tasks/{task_id}/job-board/claim")
def job_board_claim(task_id: int, request: Request) -> dict[str, Any]:
    """Open a docs-only PR that adds the task's declared scope to the project's job board."""
    return _board_call(_board(request).claim, task_id).as_dict()  # type: ignore[no-any-return]


@router.post("/tasks/{task_id}/job-board/release")
def job_board_release(task_id: int, request: Request) -> dict[str, Any]:
    """Open a docs-only PR that marks the task's job board row as released."""
    return _board_call(_board(request).release, task_id).as_dict()  # type: ignore[no-any-return]
