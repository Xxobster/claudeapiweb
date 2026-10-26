"""Gate, diff review and integration endpoints (WP10). All behind the security guard.

Reading a diff never changes the worktree. `gate` and `integrate` are explicit clicks: the gate
may start a correction run (mock tasks free, API tasks charged through the usual budget gates),
`integrate` is the only call that pushes a branch and opens a pull request.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr

from cpm.db.settings_repo import set_setting
from cpm.review.gate import GateError, GateService
from cpm.review.github import GITHUB_TOKEN, is_token_format
from cpm.review.integrate import IntegrationService
from cpm.security.secrets_store import SecretStoreUnavailable
from cpm.tasks.service import TaskError

router = APIRouter(prefix="/api", tags=["review"])


def _gate(request: Request) -> GateService:
    return request.app.state.gate  # type: ignore[no-any-return]


def _integ(request: Request) -> IntegrationService:
    return request.app.state.integration  # type: ignore[no-any-return]


def _call(fn: Any, *a: Any, **kw: Any) -> Any:
    try:
        return fn(*a, **kw)
    except TaskError as e:  # GateError, IntegrationError, NotFound, IllegalTransition
        raise HTTPException(e.status, str(e)) from e


class ApproveIn(BaseModel):
    gate_id: int = Field(ge=1)


class IntegrateIn(BaseModel):
    needs_pc: str = Field(default="", max_length=2000)


class GateConfigIn(BaseModel):
    max_corrections: int = Field(ge=0, le=5)
    auto: bool = True


@router.get("/tasks/{task_id}/review")
def review(request: Request, task_id: int) -> dict[str, Any]:
    g = _gate(request)
    integ = _call(_integ(request).get, task_id)
    return {
        "results": _call(g.results, task_id),
        "running": g.is_running(task_id),
        "integration": integ.as_dict() if integ else None,
        "max_corrections": g.max_corrections(),
    }


@router.get("/tasks/{task_id}/changes")
def changes(request: Request, task_id: int) -> dict[str, Any]:
    return _call(_gate(request).changes, task_id)  # type: ignore[no-any-return]


@router.get("/tasks/{task_id}/diff")
def diff(request: Request, task_id: int, path: str | None = None) -> dict[str, Any]:
    if path is not None and (len(path) > 500 or "\0" in path):
        raise HTTPException(422, "invalid path")
    return _call(_gate(request).diff, task_id, path)  # type: ignore[no-any-return]


@router.post("/tasks/{task_id}/gate")
def run_gate(request: Request, task_id: int) -> dict[str, Any]:
    return _call(_gate(request).run, task_id).as_dict()  # type: ignore[no-any-return]


@router.post("/tasks/{task_id}/gate/approve")
def approve(request: Request, task_id: int, body: ApproveIn) -> dict[str, Any]:
    return _call(_gate(request).approve, task_id, body.gate_id).as_dict()  # type: ignore[no-any-return]


@router.post("/tasks/{task_id}/integrate")
def integrate(request: Request, task_id: int, body: IntegrateIn | None = None) -> dict[str, Any]:
    b = body or IntegrateIn()
    return _call(_integ(request).integrate, task_id, b.needs_pc).as_dict()  # type: ignore[no-any-return]


@router.get("/gate/config")
def gate_config(request: Request) -> dict[str, Any]:
    g = _gate(request)
    return {"max_corrections": g.max_corrections(), "auto": g.auto_enabled()}


@router.put("/gate/config")
def set_gate_config(request: Request, body: GateConfigIn) -> dict[str, Any]:
    engine = request.app.state.engine
    set_setting(engine, "gate.max_corrections", body.max_corrections)
    set_setting(engine, "gate.auto", body.auto)
    return gate_config(request)


# ------------------------------------------------------------------ GitHub token (never returned)
class TokenIn(BaseModel):
    token: SecretStr


def _token_status(request: Request) -> dict[str, Any]:
    try:
        return {"configured": request.app.state.secrets.get(GITHUB_TOKEN) is not None,
                "storage_available": True}  # fmt: skip
    except SecretStoreUnavailable:
        return {"configured": False, "storage_available": False}


@router.get("/credentials/github")
def github_status(request: Request) -> dict[str, Any]:
    return _token_status(request)


@router.put("/credentials/github")
def save_github(request: Request, body: TokenIn) -> dict[str, Any]:
    value = body.token.get_secret_value().strip()
    if not is_token_format(value):
        raise HTTPException(
            422, "that does not look like a GitHub token (github_pat_... or ghp_...)"
        )
    try:
        request.app.state.secrets.set(GITHUB_TOKEN, value)
    except SecretStoreUnavailable as exc:
        raise HTTPException(503, "secure credential storage is unavailable") from exc
    return _token_status(request)


@router.delete("/credentials/github")
def remove_github(request: Request) -> dict[str, Any]:
    try:
        request.app.state.secrets.delete(GITHUB_TOKEN)
    except SecretStoreUnavailable as exc:
        raise HTTPException(503, "secure credential storage is unavailable") from exc
    return _token_status(request)


__all__ = ["router", "GateError"]
