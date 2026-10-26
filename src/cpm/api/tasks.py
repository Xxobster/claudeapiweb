"""Task, ready-queue, TODO import and prompt endpoints (WP4). All behind the security guard."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cpm.tasks import states
from cpm.tasks.planner import get_planner
from cpm.tasks.prompts import PromptStore
from cpm.tasks.service import TaskData, TaskError, TaskService
from cpm.tasks.todo_import import import_todo
from cpm.workers.supervisor import RunError

router = APIRouter(prefix="/api", tags=["tasks"])
ACTOR = "user"

Status = Literal[
    "BACKLOG", "READY", "BLOCKED", "RESERVED", "RUNNING", "TESTING", "REVIEWING",
    "CORRECTING", "PAUSED", "PAUSED_BUDGET", "FAILED", "COMPLETED", "INTEGRATED", "CANCELLED",
]  # fmt: skip
Effort = Literal["low", "medium", "high", "xhigh", "max"]
Billing = Literal["api", "cli", "mock"]


def _svc(request: Request) -> TaskService:
    return TaskService(request.app.state.engine)


def _call(fn: Any, *a: Any, **kw: Any) -> Any:
    try:
        return fn(*a, **kw)
    except TaskError as e:
        raise HTTPException(e.status, str(e)) from e
    except ValueError as e:  # parser limits
        raise HTTPException(422, str(e)) from e


def _out(t: TaskData) -> dict[str, Any]:
    return asdict(t)


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20_000)
    priority: int = Field(default=100, ge=0, le=10_000)
    role: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=64)
    effort: Effort | None = None
    billing_mode: Billing = "api"
    declared_scope: list[str] = Field(default_factory=list, max_length=200)
    acceptance: list[str] = Field(default_factory=list, max_length=200)
    estimate_microusd: int = Field(default=0, ge=0)
    depends_on: list[int] = Field(default_factory=list, max_length=200)
    parent_id: int | None = None
    lane: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    priority: int | None = Field(default=None, ge=0, le=10_000)
    role: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=64)
    effort: Effort | None = None
    billing_mode: Billing | None = None
    declared_scope: list[str] | None = Field(default=None, max_length=200)
    acceptance: list[str] | None = Field(default=None, max_length=200)
    estimate_microusd: int | None = Field(default=None, ge=0)
    lane: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")


class TransitionIn(BaseModel):
    to: Status
    reason: str | None = Field(default=None, max_length=1000)


class DepIn(BaseModel):
    depends_on_id: int


class SplitPart(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20_000)
    declared_scope: list[str] | None = Field(default=None, max_length=200)
    acceptance: list[str] = Field(default_factory=list, max_length=200)


class SplitIn(BaseModel):
    parts: list[SplitPart] = Field(min_length=2, max_length=20)


class MergeIn(BaseModel):
    task_ids: list[int] = Field(min_length=2, max_length=20)
    title: str = Field(min_length=1, max_length=300)


class TodoIn(BaseModel):
    source_name: str = Field(default="TODO.md", min_length=1, max_length=200)
    text: str = Field(max_length=300_000)


class PlanIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    live: bool = False


class PromptIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=50_000)


@router.get("/task-states")
def task_states() -> dict[str, Any]:
    return {
        "statuses": list(states.STATUSES),
        "transitions": {k: sorted(v) for k, v in states.TRANSITIONS.items()},
        "user_targets": sorted(states.USER_TARGETS),
    }


@router.get("/projects/{project_id}/tasks")
def list_tasks(request: Request, project_id: int, status: Status | None = None) -> list[Any]:
    return [_out(t) for t in _call(_svc(request).list_tasks, project_id, status)]


@router.post("/projects/{project_id}/tasks", status_code=201)
def create_task(request: Request, project_id: int, body: TaskIn) -> dict[str, Any]:
    t = _call(_svc(request).create, project_id, **body.model_dump())
    return _out(t)


@router.get("/projects/{project_id}/ready-queue")
def ready_queue(request: Request, project_id: int) -> list[Any]:
    return [_out(t) for t in _svc(request).ready_queue(project_id)]


@router.post("/projects/{project_id}/tasks/unblock")
def unblock(request: Request, project_id: int) -> dict[str, Any]:
    return {"promoted": _svc(request).unblock_ready(project_id, actor=ACTOR)}


@router.post("/projects/{project_id}/tasks/merge", status_code=201)
def merge(request: Request, project_id: int, body: MergeIn) -> dict[str, Any]:
    t = _call(_svc(request).merge, body.task_ids, body.title, actor=ACTOR, project_id=project_id)
    return _out(t)


@router.post("/projects/{project_id}/tasks/import-todo", status_code=201)
def import_todo_route(request: Request, project_id: int, body: TodoIn) -> dict[str, Any]:
    r = _call(import_todo, _svc(request), project_id, body.text, body.source_name, actor=ACTOR)
    return {
        "created": [_out(t) for t in r.created],
        "skipped_done": r.skipped_done,
        "skipped_existing": r.skipped_existing,
    }


@router.post("/projects/{project_id}/tasks/plan", status_code=201)
def plan(request: Request, project_id: int, body: PlanIn) -> dict[str, Any]:
    planner = _call(get_planner, body.live)
    svc = _svc(request)
    planned = _call(planner.plan, body.prompt)
    tasks = [
        _call(svc.create, project_id, p.title, description=p.description, source="prompt")
        for p in planned
    ]
    return {"planner": planner.name, "created": [_out(t) for t in tasks]}


@router.get("/tasks/{task_id}")
def get_task(request: Request, task_id: int) -> dict[str, Any]:
    svc = _svc(request)
    t = _call(svc.get, task_id)
    return {
        **_out(t),
        "dependents": _call(svc.dependents, task_id),
        "events": _call(svc.events, task_id),
    }


@router.patch("/tasks/{task_id}")
def edit_task(request: Request, task_id: int, body: TaskPatch) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    not_null = (
        "title", "description", "priority", "billing_mode",
        "declared_scope", "acceptance", "estimate_microusd",
    )  # fmt: skip
    if any(k in changes and changes[k] is None for k in not_null):
        raise HTTPException(422, f"{', '.join(not_null)} cannot be null")
    return _out(_call(_svc(request).edit, task_id, actor=ACTOR, **changes))


def _via_worker(request: Request, task_id: int, to: str) -> dict[str, Any] | None:
    """A pause/cancel of a task with a live worker must stop that worker first (WP5).

    Otherwise the task would say CANCELLED while its process keeps running and editing files.
    The supervisor moves the task itself once the process tree is gone.
    """
    sup = request.app.state.supervisor
    run_id = sup.live_run_for_task(task_id)
    if run_id is None or to not in ("PAUSED", "CANCELLED"):
        return None
    try:
        sup.stop(run_id, "cancel" if to == "CANCELLED" else "pause")
    except RunError as e:
        raise HTTPException(e.status, str(e)) from e
    sup.wait(run_id, 15.0)
    return _out(_svc(request).get(task_id))


@router.post("/tasks/{task_id}/transition")
def transition(request: Request, task_id: int, body: TransitionIn) -> dict[str, Any]:
    """Human-requested moves only. Worker/gate-driven states are refused here (409)."""
    if body.to not in states.USER_TARGETS:
        raise HTTPException(409, f"{body.to} is set by the scheduler, workers or acceptance gate")
    if (stopped := _via_worker(request, task_id, body.to)) is not None:
        return stopped
    svc = _svc(request)
    if body.to == "READY":
        return _out(_call(svc.mark_ready, task_id, actor=ACTOR))
    return _out(_call(svc.transition, task_id, body.to, actor=ACTOR, reason=body.reason))


@router.post("/tasks/{task_id}/cancel")
def cancel(request: Request, task_id: int, body: TransitionIn | None = None) -> dict[str, Any]:
    reason = body.reason if body else None
    if (stopped := _via_worker(request, task_id, "CANCELLED")) is not None:
        return stopped
    return _out(_call(_svc(request).cancel, task_id, actor=ACTOR, reason=reason))


@router.post("/tasks/{task_id}/dependencies")
def add_dep(request: Request, task_id: int, body: DepIn) -> dict[str, Any]:
    return _out(_call(_svc(request).add_dependency, task_id, body.depends_on_id, actor=ACTOR))


@router.delete("/tasks/{task_id}/dependencies/{dep_id}")
def remove_dep(request: Request, task_id: int, dep_id: int) -> dict[str, Any]:
    return _out(_call(_svc(request).remove_dependency, task_id, dep_id, actor=ACTOR))


@router.post("/tasks/{task_id}/split", status_code=201)
def split(request: Request, task_id: int, body: SplitIn) -> list[Any]:
    parts = [p.model_dump() for p in body.parts]
    return [_out(t) for t in _call(_svc(request).split, task_id, parts, actor=ACTOR)]


@router.get("/prompts")
def list_prompts(request: Request) -> list[dict[str, Any]]:
    return [asdict(p) for p in PromptStore(request.app.state.engine).list_latest()]


@router.get("/prompts/{name}")
def get_prompt(request: Request, name: str, version: int | None = None) -> dict[str, Any]:
    return asdict(_call(PromptStore(request.app.state.engine).get, name, version))


@router.post("/prompts", status_code=201)
def save_prompt(request: Request, body: PromptIn) -> dict[str, Any]:
    return asdict(_call(PromptStore(request.app.state.engine).save, body.name, body.body))
