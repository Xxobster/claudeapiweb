"""Scheduler control (WP7): status, pause/resume, one manual tick, limits and retry bounds.

Starting workers is never done here directly: `tick` runs the same deterministic pass the loop
does, so a manual tick cannot skip reservations, locks or budget admission. Behind the usual
token/Host/Origin/CSRF guard.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from cpm.scheduler.core import Scheduler

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


def _sched(request: Request) -> Scheduler:
    return request.app.state.scheduler  # type: ignore[no-any-return]


class ConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    per_project_limit: int | None = Field(default=None, ge=1, le=8)
    max_auto_retries: int = Field(default=2, ge=0, le=5)
    identical_failure_limit: int = Field(default=2, ge=1, le=5)
    retry_delay_s: float = Field(default=30.0, ge=0, le=3600)
    hang_timeout_s: float = Field(default=120.0, ge=1, le=3600)
    max_runtime_s: float = Field(default=3600.0, ge=1, le=86_400)
    external_scan_s: float = Field(default=30.0, ge=1, le=3600)
    retry_billing_modes: list[str] = Field(default_factory=lambda: ["mock"], max_length=3)


@router.get("")
def status(request: Request) -> dict[str, Any]:
    out = _sched(request).status()
    out["enabled"] = request.app.state.settings.scheduler_enabled
    return out


@router.get("/queue")
def queue(request: Request) -> dict[str, Any]:
    """READY queue in dispatch order with the reason each task waits (read-only)."""
    return _sched(request).queue()


@router.post("/pause")
def pause(request: Request) -> dict[str, Any]:
    """Stop starting new work. Running workers are untouched (cancel/pause them per task)."""
    _sched(request).set_paused(True)
    return {"paused": True}


@router.post("/resume")
def resume(request: Request) -> dict[str, Any]:
    _sched(request).set_paused(False)
    return {"paused": False}


@router.post("/tick")
def tick(request: Request) -> dict[str, Any]:
    return _sched(request).tick().as_dict()


@router.put("/config")
def put_config(request: Request, body: ConfigIn) -> dict[str, Any]:
    if any(m not in ("mock", "api", "cli") for m in body.retry_billing_modes):
        raise HTTPException(422, "unknown billing mode")
    return _sched(request).set_config(body.model_dump()).dump()
