"""Lane endpoints (WP12): what a project's lanes allow, and tasks created from a lane template.

Read-only except task creation, which goes through `TaskService.create` (so the lane must be one
the profile defines and the task starts in BACKLOG like any other). Behind the usual
token/Host/Origin/CSRF guard.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from cpm.lanes.registry import validator_kinds
from cpm.lanes.service import LaneService
from cpm.lanes.spec import LANE_ID
from cpm.tasks.service import TaskError, TaskService

router = APIRouter(prefix="/api", tags=["lanes"])
ACTOR = "user"
MAX_INPUT = 500


class LaneTaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    inputs: dict[str, str] = Field(default_factory=dict, max_length=16)
    priority: int = Field(default=100, ge=0, le=10_000)
    billing_mode: str = Field(default="api", pattern="^(api|cli|mock)$")
    estimate_microusd: int = Field(default=0, ge=0)
    model: str | None = Field(default=None, max_length=64)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


def _svc(request: Request) -> LaneService:
    return request.app.state.lanes  # type: ignore[no-any-return]


@router.get("/lanes/validators")
def validators() -> dict[str, Any]:
    """Validator kinds a profile may reference (built in + registered by lane modules)."""
    return {"validators": validator_kinds()}


@router.get("/projects/{project_id}/lanes")
def project_lanes(request: Request, project_id: int) -> dict[str, Any]:
    svc = _svc(request)
    return {
        "lanes": [
            svc.describe_lane(lid, spec) for lid, spec in svc.project_lanes(project_id).items()
        ]
    }


@router.post("/projects/{project_id}/lanes/{lane_id}/tasks", status_code=201)
def create_lane_task(
    request: Request, project_id: int, lane_id: str, body: LaneTaskIn
) -> dict[str, Any]:
    svc = _svc(request)
    if not LANE_ID.match(lane_id):
        raise HTTPException(422, "invalid lane name")
    spec = svc.project_lanes(project_id).get(lane_id)
    if spec is None:
        raise HTTPException(404, f"lane {lane_id!r} not found")
    if not spec.enabled:
        raise HTTPException(409, f"lane {lane_id!r} is disabled")
    tmpl = spec.task_template
    missing = [n for n in tmpl.required_inputs if not body.inputs.get(n, "").strip()]
    unknown = [n for n in body.inputs if n not in tmpl.required_inputs]
    if missing or unknown:
        raise HTTPException(422, f"inputs: missing {missing}, not accepted {unknown}")
    if any(len(v) > MAX_INPUT for v in body.inputs.values()):
        raise HTTPException(422, f"an input is longer than {MAX_INPUT} characters")
    title = (tmpl.title_prefix + body.title).strip()
    try:
        task = TaskService(request.app.state.engine).create(
            project_id,
            title,
            description=svc.template_description(spec, body.inputs),
            priority=body.priority,
            model=body.model,
            effort=body.effort,
            billing_mode=body.billing_mode,
            declared_scope=tmpl.default_scope or spec.artifacts.asset_paths,
            acceptance=tmpl.default_acceptance,
            estimate_microusd=body.estimate_microusd,
            lane=lane_id,
        )
    except TaskError as e:
        raise HTTPException(e.status, str(e)) from e
    return asdict(task)
