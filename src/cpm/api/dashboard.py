"""Dashboard summary (WP15): real counts from the database, no placeholders.

One call gives the UI everything the global dashboard needs: per project task counts by status,
the workers running now and the recorded spend (client-side estimate vs reconciled, never mixed
without a label). Budgets and reservations (WP6-WP8) are separate endpoints the UI probes for.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cpm.db.models import Project, Run, Task, UsageEvent
from cpm.workers.supervisor import RunSupervisor

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard")
def dashboard(request: Request) -> dict[str, Any]:
    sup: RunSupervisor = request.app.state.supervisor
    with Session(request.app.state.engine) as s:
        projects = list(s.scalars(select(Project).order_by(Project.name)))
        counts: dict[int, dict[str, int]] = {p.id: {} for p in projects}
        for pid, status, n in s.execute(
            select(Task.project_id, Task.status, func.count()).group_by(
                Task.project_id, Task.status
            )
        ):
            counts.setdefault(pid, {})[status] = n
        spend: dict[int, dict[str, int]] = {}
        for pid, reconciled, total in s.execute(
            select(
                UsageEvent.project_id, UsageEvent.reconciled, func.sum(UsageEvent.cost_microusd)
            ).group_by(UsageEvent.project_id, UsageEvent.reconciled)
        ):
            spend.setdefault(pid, {})["reconciled" if reconciled else "estimated"] = int(total or 0)
        active = []
        for run, task in s.execute(
            select(Run, Task).join(Task, Task.id == Run.task_id).where(Run.ended_at.is_(None))
        ):
            if not sup.is_live(run.id):
                continue
            active.append(
                {
                    "run_id": run.id,
                    "task_id": task.id,
                    "task_title": task.title,
                    "project_id": task.project_id,
                    "attempt": run.attempt,
                    "model": run.model,
                    "billing_mode": run.billing_mode,
                    "status": run.status,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                    "heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else None,
                }
            )
        return {
            "projects": [
                {
                    "id": p.id,
                    "name": p.name,
                    "workspace_trusted": p.workspace_trusted,
                    "task_counts": counts.get(p.id, {}),
                    "spend_microusd": {
                        "estimated": spend.get(p.id, {}).get("estimated", 0),
                        "reconciled": spend.get(p.id, {}).get("reconciled", 0),
                    },
                }
                for p in projects
            ],
            "active_runs": active,
        }
