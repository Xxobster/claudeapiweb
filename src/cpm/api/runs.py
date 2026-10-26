"""Run endpoints (WP5): start/stop/pause/resume a worker, read its log, stream it live.

Everything is behind the security guard (token, Host/Origin, CSRF; the WebSocket also needs the
token header and an allowed Origin). Starting a run of a `mock` task needs nothing; an `api` task
passes the live-run gates in `RunSupervisor._live_gates`. Run logs contain redacted event
metadata, never the API key.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from cpm.db.models import Project
from cpm.workers.supervisor import RunError, RunInfo, RunSupervisor
from cpm.workers.worktree import WorktreeError, WorktreeHasChanges

router = APIRouter(prefix="/api", tags=["runs"])
POLL_S = 0.2


def _sup(request: Request) -> RunSupervisor:
    return request.app.state.supervisor  # type: ignore[no-any-return]


def _out(r: RunInfo) -> dict[str, Any]:
    return asdict(r)


class StartIn(BaseModel):
    scenario: str = Field(default="success", max_length=32)
    scenario_args: dict[str, int | float | str] = Field(default_factory=dict, max_length=10)
    max_budget_microusd: int | None = Field(default=None, ge=1, le=1_000_000_000)
    hang_timeout_s: float = Field(default=60.0, ge=0.5, le=3600)
    max_runtime_s: float = Field(default=3600.0, ge=1, le=86_400)


def _call(fn: Any, *a: Any, **kw: Any) -> Any:
    try:
        return fn(*a, **kw)
    except RunError as e:
        raise HTTPException(e.status, str(e)) from e
    except WorktreeHasChanges as e:
        raise HTTPException(409, str(e)) from e
    except WorktreeError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/tasks/{task_id}/runs", status_code=201)
def start_run(request: Request, task_id: int, body: StartIn | None = None) -> dict[str, Any]:
    b = body or StartIn()
    return _out(_call(
        _sup(request).start, task_id, scenario=b.scenario, scenario_args=b.scenario_args,
        max_budget_microusd=b.max_budget_microusd, hang_timeout_s=b.hang_timeout_s,
        max_runtime_s=b.max_runtime_s,
    ))  # fmt: skip


@router.get("/tasks/{task_id}/runs")
def list_runs(request: Request, task_id: int) -> list[Any]:
    return [_out(r) for r in _sup(request).list_runs(task_id)]


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: int) -> dict[str, Any]:
    return _out(_call(_sup(request).get_run, run_id))


@router.get("/runs/{run_id}/events")
def run_events(request: Request, run_id: int, since: int = 0, limit: int = 500) -> dict[str, Any]:
    sup = _sup(request)
    _call(sup.get_run, run_id)
    return {"live": sup.is_live(run_id), "events": sup.run_log(run_id).read(since, limit)}


@router.post("/runs/{run_id}/stop")
def stop_run(request: Request, run_id: int) -> dict[str, Any]:
    """Cancel: kill the worker's process tree now; the task becomes CANCELLED."""
    _call(_sup(request).stop, run_id, "cancel")
    return {"requested": "cancel"}


@router.post("/runs/{run_id}/pause")
def pause_run(request: Request, run_id: int) -> dict[str, Any]:
    """Pause: ask the worker to stop, then kill after a short grace; the task becomes PAUSED."""
    _call(_sup(request).stop, run_id, "pause")
    return {"requested": "pause"}


@router.post("/tasks/{task_id}/resume", status_code=201)
def resume_task(request: Request, task_id: int, body: StartIn | None = None) -> dict[str, Any]:
    """Resume a PAUSED task: a new run continues the same SDK session in the same worktree."""
    return start_run(request, task_id, body)


@router.delete("/tasks/{task_id}/worktree")
def delete_worktree(request: Request, task_id: int) -> dict[str, Any]:
    """Delete the task's worktree only if it is clean and holds no unmerged commits."""
    sup = _sup(request)
    if sup.busy(task_id):
        raise HTTPException(409, "the task has a running worker")
    task = sup.tasks.get(task_id)
    with Session(sup.engine) as s:
        project = s.get(Project, task.project_id)
        if project is None:
            raise HTTPException(404, "project not found")
        path, slug = project.path, project.slug
    removed = _call(sup.worktrees.remove, Path(path), slug, task_id)
    return {"removed": removed}


@router.websocket("/runs/{run_id}/stream")
async def stream(ws: WebSocket, run_id: int, since: int = 0) -> None:
    """Live event stream. Replays from `since`, then follows the log until the run ends."""
    sup: RunSupervisor = ws.app.state.supervisor
    try:
        sup.get_run(run_id)
    except RunError:
        await ws.close(code=1008)
        return
    await ws.accept()
    log = sup.run_log(run_id)
    last = since
    try:
        while True:
            live = sup.is_live(run_id)
            events = log.read(last, 200)
            for ev in events:
                await ws.send_json(ev)
                last = ev["seq"]
            if not live and not events:
                await ws.send_json({"type": "end", "seq": last})
                await ws.close()
                return
            await asyncio.sleep(POLL_S)
    except WebSocketDisconnect:
        return
