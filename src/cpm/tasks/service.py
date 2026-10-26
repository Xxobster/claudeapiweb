"""Task service: CRUD, state machine, dependency DAG, ready queue, split/merge (WP4).

All status changes go through `transition`, which checks `states.TRANSITIONS`, uses a
compare-and-set UPDATE (so two racing callers cannot both win) and appends an `events` row.
Graph mutations (add dependency, split, merge) are serialized by a process-wide lock: CPM is a
single-process server, and the cycle check + insert must be atomic.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, Engine, Result, delete, select, update
from sqlalchemy.orm import Session

from cpm.db.models import Event, Project, Task, TaskDep, utcnow
from cpm.security.paths import PathPolicyError, validate_relative
from cpm.security.redaction import redact
from cpm.tasks import states

MAX_TITLE = 300
MAX_TEXT = 20_000
MAX_SCOPE_ITEMS = 200
MAX_PRIORITY = 10_000
EFFORTS = ("low", "medium", "high", "xhigh", "max")
BILLING = ("api", "cli", "mock")
_graph_lock = threading.RLock()


def _rowcount(res: Result[Any]) -> int:
    return cast(CursorResult[Any], res).rowcount


class TaskError(Exception):
    """Base class; `.status` is the HTTP status the API maps it to."""

    status = 400


class NotFound(TaskError):
    status = 404


class IllegalTransition(TaskError):
    status = 409


class CycleError(TaskError):
    status = 409


class InvalidTask(TaskError):
    status = 422


@dataclass
class TaskData:
    """Plain snapshot (sessions are closed when the service returns)."""

    id: int
    project_id: int
    title: str
    description: str
    status: str
    priority: int
    role: str | None
    model: str | None
    effort: str | None
    billing_mode: str
    declared_scope: list[str]
    acceptance: list[str]
    estimate_microusd: int
    block_reason: str | None
    parent_id: int | None
    retry_count: int
    source: str
    depends_on: list[int] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


def _clean_text(value: str, limit: int, what: str) -> str:
    if len(value) > limit:
        raise InvalidTask(f"{what} longer than {limit} characters")
    # Control characters (except newline/tab) have no place in titles or notes.
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32).strip()


def clean_title(value: str) -> str:
    v = _clean_text(value, MAX_TITLE, "title").replace("\n", " ")
    if not v:
        raise InvalidTask("title is empty")
    return v


def clean_scope(items: Sequence[str]) -> list[str]:
    if len(items) > MAX_SCOPE_ITEMS:
        raise InvalidTask(f"more than {MAX_SCOPE_ITEMS} scope entries")
    out: list[str] = []
    for raw in items:
        try:
            p = validate_relative(raw)
        except PathPolicyError as e:
            raise InvalidTask(str(e)) from e
        if p not in out:
            out.append(p)
    return out


def clean_acceptance(items: Sequence[str]) -> list[str]:
    if len(items) > MAX_SCOPE_ITEMS:
        raise InvalidTask("too many acceptance criteria")
    return [c for c in (_clean_text(i, 1000, "acceptance criterion") for i in items) if c]


def _check_choice(value: str | None, allowed: Iterable[str], what: str) -> None:
    if value is not None and value not in tuple(allowed):
        raise InvalidTask(f"{what} must be one of {', '.join(allowed)}")


def _check_priority(p: int) -> int:
    if not 0 <= p <= MAX_PRIORITY:
        raise InvalidTask(f"priority must be between 0 and {MAX_PRIORITY} (lower runs first)")
    return p


def _check_estimate(v: int) -> int:
    if v < 0:
        raise InvalidTask("estimate cannot be negative")
    return v


class TaskService:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ---------------------------------------------------------------- helpers
    def _log(self, s: Session, task: Task, kind: str, **detail: Any) -> None:
        s.add(
            Event(
                project_id=task.project_id,
                task_id=task.id,
                kind=kind,
                detail=redact(json.dumps(detail, default=str)),
            )
        )

    def _get(self, s: Session, task_id: int) -> Task:
        t = s.get(Task, task_id)
        if t is None:
            raise NotFound(f"task {task_id} not found")
        return t

    def _deps(self, s: Session, task_id: int) -> list[int]:
        return sorted(s.scalars(select(TaskDep.depends_on_id).where(TaskDep.task_id == task_id)))

    def _dependents(self, s: Session, task_id: int) -> list[int]:
        return sorted(s.scalars(select(TaskDep.task_id).where(TaskDep.depends_on_id == task_id)))

    def _snap(self, s: Session, t: Task) -> TaskData:
        return TaskData(
            id=t.id,
            project_id=t.project_id,
            title=t.title,
            description=t.description,
            status=t.status,
            priority=t.priority,
            role=t.role,
            model=t.model,
            effort=t.effort,
            billing_mode=t.billing_mode,
            declared_scope=json.loads(t.declared_scope),
            acceptance=json.loads(t.acceptance),
            estimate_microusd=t.estimate_microusd,
            block_reason=t.block_reason,
            parent_id=t.parent_id,
            retry_count=t.retry_count,
            source=t.source,
            depends_on=self._deps(s, t.id),
            created_at=t.created_at,
            updated_at=t.updated_at,
            started_at=t.started_at,
            completed_at=t.completed_at,
        )

    def _reaches(self, s: Session, start: int, target: int) -> bool:
        """True if `target` is reachable from `start` following depends-on edges (iterative DFS)."""
        seen: set[int] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(s.scalars(select(TaskDep.depends_on_id).where(TaskDep.task_id == cur)))
        return False

    def _add_dep(self, s: Session, task: Task, dep: Task) -> bool:
        if task.id == dep.id:
            raise CycleError("a task cannot depend on itself")
        if task.project_id != dep.project_id:
            raise InvalidTask("dependencies must be in the same project")
        exists = s.get(TaskDep, (task.id, dep.id))
        if exists:
            return False
        if self._reaches(s, dep.id, task.id):
            raise CycleError(f"dependency {task.id} -> {dep.id} would create a cycle")
        s.add(TaskDep(task_id=task.id, depends_on_id=dep.id))
        s.flush()
        return True

    # ---------------------------------------------------------------- create / read
    def create(
        self,
        project_id: int,
        title: str,
        *,
        description: str = "",
        priority: int = 100,
        role: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        billing_mode: str = "api",
        declared_scope: Sequence[str] = (),
        acceptance: Sequence[str] = (),
        estimate_microusd: int = 0,
        depends_on: Sequence[int] = (),
        parent_id: int | None = None,
        source: str = "manual",
        source_ref: str | None = None,
    ) -> TaskData:
        _check_choice(effort, EFFORTS, "effort")
        _check_choice(billing_mode, BILLING, "billing_mode")
        _check_choice(source, ("manual", "prompt", "todo"), "source")
        with _graph_lock, Session(self.engine) as s:
            if s.get(Project, project_id) is None:
                raise NotFound(f"project {project_id} not found")
            if parent_id is not None and self._get(s, parent_id).project_id != project_id:
                raise InvalidTask("parent must be in the same project")
            t = Task(
                project_id=project_id,
                title=clean_title(title),
                description=_clean_text(description, MAX_TEXT, "description"),
                priority=_check_priority(priority),
                role=role,
                model=model,
                effort=effort,
                billing_mode=billing_mode,
                declared_scope=json.dumps(clean_scope(declared_scope)),
                acceptance=json.dumps(clean_acceptance(acceptance)),
                estimate_microusd=_check_estimate(estimate_microusd),
                parent_id=parent_id,
                source=source,
                source_ref=source_ref,
                status="BACKLOG",
            )
            s.add(t)
            s.flush()
            for d in dict.fromkeys(depends_on):
                self._add_dep(s, t, self._get(s, d))
            self._log(
                s, t, "task.created", title=t.title, source=source, depends_on=list(depends_on)
            )
            s.commit()
            return self._snap(s, t)

    def get(self, task_id: int) -> TaskData:
        with Session(self.engine) as s:
            return self._snap(s, self._get(s, task_id))

    def list_tasks(self, project_id: int, status: str | None = None) -> list[TaskData]:
        if status is not None:
            _check_choice(status, states.STATUSES, "status")
        with Session(self.engine) as s:
            q = select(Task).where(Task.project_id == project_id)
            if status:
                q = q.where(Task.status == status)
            q = q.order_by(Task.priority, Task.id)
            return [self._snap(s, t) for t in s.scalars(q)]

    def dependents(self, task_id: int) -> list[int]:
        with Session(self.engine) as s:
            self._get(s, task_id)
            return self._dependents(s, task_id)

    def events(self, task_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with Session(self.engine) as s:
            self._get(s, task_id)
            rows = s.scalars(
                select(Event).where(Event.task_id == task_id).order_by(Event.id.desc()).limit(limit)
            )
            return [
                {"id": e.id, "ts": e.ts, "kind": e.kind, "detail": json.loads(e.detail)}
                for e in rows
            ]

    # ---------------------------------------------------------------- state machine
    def transition(
        self,
        task_id: int,
        to: str,
        *,
        actor: str,
        reason: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> TaskData:
        """Move a task along `states.TRANSITIONS`.

        COMPLETED needs `evidence={"acceptance_gate": "passed"}` from the gate (WP10), so
        nothing can be completed silently. READY needs every dependency done; use `mark_ready`
        for the "READY or BLOCKED" decision.
        """
        _check_choice(to, states.STATUSES, "status")
        with _graph_lock, Session(self.engine) as s:
            t = self._get(s, task_id)
            src = t.status
            if not states.can_transition(src, to):
                raise IllegalTransition(f"{src} -> {to} is not allowed")
            if to == "COMPLETED" and (evidence or {}).get("acceptance_gate") != "passed":
                raise IllegalTransition("COMPLETED requires a passed acceptance gate")
            if to == "READY" and not self._deps_done(s, task_id):
                raise IllegalTransition("READY requires all dependencies COMPLETED or INTEGRATED")
            now = utcnow()
            values: dict[str, Any] = {"status": to, "updated_at": now}
            if to in ("BLOCKED",):
                values["block_reason"] = _clean_text(reason or "blocked", 1000, "reason")
            elif to in ("READY", "BACKLOG", "RUNNING"):
                values["block_reason"] = None
            if to == "RUNNING" and t.started_at is None:
                values["started_at"] = now
            if to in ("COMPLETED", "CANCELLED"):
                values["completed_at"] = now
            if src == "FAILED" and to in ("READY", "BACKLOG"):
                values["retry_count"] = t.retry_count + 1
            res = s.execute(
                update(Task).where(Task.id == task_id, Task.status == src).values(**values)
            )
            if _rowcount(res) != 1:  # lost a race with another transition
                raise IllegalTransition("task status changed concurrently; reload and retry")
            s.expire(t)
            self._log(
                s, t, "task.transition", src=src, dst=to, actor=actor,
                reason=reason, evidence=evidence,
            )  # fmt: skip
            if to == "CANCELLED":
                self._block_dependents(s, task_id, f"dependency #{task_id} was cancelled")
            s.commit()
            return self._snap(s, t)

    def _deps_done(self, s: Session, task_id: int) -> bool:
        statuses = s.scalars(
            select(Task.status)
            .join(TaskDep, TaskDep.depends_on_id == Task.id)
            .where(TaskDep.task_id == task_id)
        )
        return all(st in states.DONE for st in statuses)

    def _block_dependents(self, s: Session, task_id: int, why: str) -> None:
        for dep_id in self._dependents(s, task_id):
            d = s.get(Task, dep_id)
            if d is not None and d.status in ("BACKLOG", "READY"):
                d.status = "BLOCKED"
                d.block_reason = why
                self._log(
                    s, d, "task.transition", src="auto", dst="BLOCKED", actor="system", reason=why
                )

    def mark_ready(self, task_id: int, *, actor: str) -> TaskData:
        """Promote to READY, or to BLOCKED (with the reason) when dependencies are not done."""
        with _graph_lock, Session(self.engine) as s:
            t = self._get(s, task_id)
            unmet = [
                d for d in self._deps(s, task_id)
                if s.get(Task, d).status not in states.DONE  # type: ignore[union-attr]
            ]  # fmt: skip
        if not unmet:
            return self.transition(task_id, "READY", actor=actor)
        if t.status == "BLOCKED":
            return self.get(task_id)
        return self.transition(task_id, "BLOCKED", actor=actor, reason=f"waiting for tasks {unmet}")

    def unblock_ready(self, project_id: int, *, actor: str = "system") -> list[int]:
        """Promote BLOCKED tasks whose dependencies are now all done. Returns promoted ids."""
        promoted: list[int] = []
        with Session(self.engine) as s:
            ids = list(
                s.scalars(
                    select(Task.id).where(Task.project_id == project_id, Task.status == "BLOCKED")
                )
            )
        for tid in ids:
            try:
                self.transition(tid, "READY", actor=actor, reason="dependencies done")
                promoted.append(tid)
            except IllegalTransition:
                continue
        return promoted

    def ready_queue(self, project_id: int | None = None) -> list[TaskData]:
        """READY tasks whose dependencies are all done, best first (priority, then age)."""
        with Session(self.engine) as s:
            q = select(Task).where(Task.status == "READY")
            if project_id is not None:
                q = q.where(Task.project_id == project_id)
            q = q.order_by(Task.priority, Task.id)
            return [self._snap(s, t) for t in s.scalars(q) if self._deps_done(s, t.id)]

    # ---------------------------------------------------------------- edit
    def edit(self, task_id: int, *, actor: str, **changes: Any) -> TaskData:
        """Edit fields. Scope, model, effort and billing mode only while no worker is attached."""
        allowed = {
            "title", "description", "priority", "role", "model", "effort", "billing_mode",
            "declared_scope", "acceptance", "estimate_microusd",
        }  # fmt: skip
        unknown = set(changes) - allowed
        if unknown:
            raise InvalidTask(f"cannot edit: {sorted(unknown)}")
        locked = {"declared_scope", "model", "effort", "billing_mode", "role", "estimate_microusd"}
        with _graph_lock, Session(self.engine) as s:
            t = self._get(s, task_id)
            if t.status in states.TERMINAL or t.status in ("COMPLETED",):
                raise IllegalTransition(f"{t.status} tasks cannot be edited")
            if t.status in states.ACTIVE and locked & set(changes):
                raise IllegalTransition(
                    f"{sorted(locked & set(changes))} cannot change while the task is {t.status}"
                )
            if "title" in changes:
                t.title = clean_title(changes["title"])
            if "description" in changes:
                t.description = _clean_text(changes["description"], MAX_TEXT, "description")
            if "priority" in changes:
                t.priority = _check_priority(changes["priority"])
            if "role" in changes:
                t.role = changes["role"]
            if "model" in changes:
                t.model = changes["model"]
            if "effort" in changes:
                _check_choice(changes["effort"], EFFORTS, "effort")
                t.effort = changes["effort"]
            if "billing_mode" in changes:
                _check_choice(changes["billing_mode"], BILLING, "billing_mode")
                t.billing_mode = changes["billing_mode"]
            if "declared_scope" in changes:
                t.declared_scope = json.dumps(clean_scope(changes["declared_scope"]))
            if "acceptance" in changes:
                t.acceptance = json.dumps(clean_acceptance(changes["acceptance"]))
            if "estimate_microusd" in changes:
                t.estimate_microusd = _check_estimate(changes["estimate_microusd"])
            self._log(s, t, "task.edited", actor=actor, fields=sorted(changes))
            s.commit()
            return self._snap(s, t)

    def reprioritize(self, task_id: int, priority: int, *, actor: str) -> TaskData:
        return self.edit(task_id, actor=actor, priority=priority)

    def reassign(
        self, task_id: int, *, actor: str,
        role: str | None = None, model: str | None = None, effort: str | None = None,
    ) -> TaskData:  # fmt: skip
        changes = {k: v for k, v in (("role", role), ("model", model), ("effort", effort)) if v}
        return self.edit(task_id, actor=actor, **changes)

    def cancel(self, task_id: int, *, actor: str, reason: str | None = None) -> TaskData:
        return self.transition(task_id, "CANCELLED", actor=actor, reason=reason)

    # ---------------------------------------------------------------- dependencies
    def add_dependency(self, task_id: int, depends_on_id: int, *, actor: str) -> TaskData:
        with _graph_lock, Session(self.engine) as s:
            t = self._get(s, task_id)
            if t.status in states.ACTIVE or t.status in ("COMPLETED", "INTEGRATED"):
                raise IllegalTransition(f"dependencies cannot change while the task is {t.status}")
            added = self._add_dep(s, t, self._get(s, depends_on_id))
            if added:
                self._log(s, t, "task.dep_added", actor=actor, depends_on=depends_on_id)
                dep = self._get(s, depends_on_id)
                if t.status == "READY" and dep.status not in states.DONE:
                    t.status = "BLOCKED"
                    t.block_reason = f"waiting for tasks [{depends_on_id}]"
            s.commit()
            return self._snap(s, t)

    def remove_dependency(self, task_id: int, depends_on_id: int, *, actor: str) -> TaskData:
        with _graph_lock, Session(self.engine) as s:
            t = self._get(s, task_id)
            res = s.execute(
                delete(TaskDep).where(
                    TaskDep.task_id == task_id, TaskDep.depends_on_id == depends_on_id
                )
            )
            if _rowcount(res):
                self._log(s, t, "task.dep_removed", actor=actor, depends_on=depends_on_id)
            s.commit()
            return self._snap(s, t)

    # ---------------------------------------------------------------- split / merge
    _SPLITTABLE = frozenset({"BACKLOG", "READY", "BLOCKED"})

    def split(self, task_id: int, parts: Sequence[dict[str, Any]], *, actor: str) -> list[TaskData]:
        """Replace a task by 2+ subtasks.

        Each subtask inherits the dependencies, priority, model settings and (unless it gives
        its own) the scope of the original. Tasks that depended on the original now depend on
        every subtask. The original is cancelled with a pointer to its parts.
        """
        if len(parts) < 2:
            raise InvalidTask("a split needs at least two parts")
        with _graph_lock, Session(self.engine) as s:
            orig = self._get(s, task_id)
            if orig.status not in self._SPLITTABLE:
                raise IllegalTransition(f"{orig.status} tasks cannot be split")
            deps = self._deps(s, task_id)
            dependents = self._dependents(s, task_id)
            new: list[Task] = []
            for p in parts:
                t = Task(
                    project_id=orig.project_id,
                    title=clean_title(p["title"]),
                    description=_clean_text(p.get("description", ""), MAX_TEXT, "description"),
                    priority=orig.priority,
                    role=orig.role,
                    model=orig.model,
                    effort=orig.effort,
                    billing_mode=orig.billing_mode,
                    declared_scope=json.dumps(
                        clean_scope(p["declared_scope"])
                        if p.get("declared_scope") is not None
                        else json.loads(orig.declared_scope)
                    ),
                    acceptance=json.dumps(clean_acceptance(p.get("acceptance", []))),
                    parent_id=orig.id,
                    source=orig.source,
                    status="BACKLOG",
                )
                s.add(t)
                s.flush()
                for d in deps:
                    self._add_dep(s, t, self._get(s, d))
                new.append(t)
            s.execute(delete(TaskDep).where(TaskDep.depends_on_id == orig.id))
            for dep_id in dependents:
                for t in new:
                    self._add_dep(s, self._get(s, dep_id), t)
            ids = [t.id for t in new]
            orig.status = "CANCELLED"
            orig.block_reason = f"split into {ids}"
            orig.completed_at = utcnow()
            orig.source_ref = None
            self._log(s, orig, "task.split", actor=actor, parts=ids)
            for t in new:
                self._log(s, t, "task.created", title=t.title, split_from=orig.id)
            s.commit()
            return [self._snap(s, t) for t in new]

    def merge(
        self, task_ids: Sequence[int], title: str, *, actor: str, project_id: int
    ) -> TaskData:
        """Merge 2+ not-started tasks of one project into a new task (originals are cancelled)."""
        ids = list(dict.fromkeys(task_ids))
        if len(ids) < 2:
            raise InvalidTask("a merge needs at least two distinct tasks")
        with _graph_lock, Session(self.engine) as s:
            olds = [self._get(s, i) for i in ids]
            for o in olds:
                if o.project_id != project_id:
                    raise InvalidTask("all merged tasks must belong to the project")
                if o.status not in self._SPLITTABLE:
                    raise IllegalTransition(f"task {o.id} is {o.status} and cannot be merged")
            idset = set(ids)
            deps = sorted({d for o in olds for d in self._deps(s, o.id)} - idset)
            dependents = sorted({d for o in olds for d in self._dependents(s, o.id)} - idset)
            scope: list[str] = []
            acceptance: list[str] = []
            for o in olds:
                scope += [p for p in json.loads(o.declared_scope) if p not in scope]
                acceptance += [a for a in json.loads(o.acceptance) if a not in acceptance]
            first = olds[0]
            new = Task(
                project_id=project_id,
                title=clean_title(title),
                description=_clean_text(
                    "\n\n".join(f"[#{o.id}] {o.title}\n{o.description}".strip() for o in olds),
                    MAX_TEXT,
                    "description",
                ),  # fmt: skip
                priority=min(o.priority for o in olds),
                role=first.role,
                model=first.model,
                effort=first.effort,
                billing_mode=first.billing_mode,
                declared_scope=json.dumps(scope),
                acceptance=json.dumps(acceptance),
                estimate_microusd=sum(o.estimate_microusd for o in olds),
                source="manual",
                status="BACKLOG",
            )
            s.add(new)
            s.flush()
            for d in deps:
                self._add_dep(s, new, self._get(s, d))
            s.execute(
                delete(TaskDep).where(TaskDep.depends_on_id.in_(ids) | TaskDep.task_id.in_(ids))
            )
            for dep_id in dependents:
                self._add_dep(s, self._get(s, dep_id), new)  # CycleError rolls everything back
            for o in olds:
                o.status = "CANCELLED"
                o.block_reason = f"merged into #{new.id}"
                o.completed_at = utcnow()
                o.source_ref = None
                self._log(s, o, "task.merged", actor=actor, into=new.id)
            self._log(s, new, "task.created", title=new.title, merged_from=ids)
            s.commit()
            return self._snap(s, new)
