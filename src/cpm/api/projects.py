"""Project registry API (WP3). Registers managed projects read-only; never writes into them."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from cpm.db.models import Event, Project, ProjectSource, Setting
from cpm.db.settings_repo import get_setting, set_setting
from cpm.discovery import gitinfo, scan
from cpm.discovery.commands import detect_commands, detect_unity
from cpm.discovery.policy import worker_policy
from cpm.profiles import Profile, ProfileError, list_profiles, load_profile
from cpm.security.paths import PathPolicyError, validate_root

router = APIRouter(prefix="/api", tags=["projects"])
_SLUG = re.compile(r"[^a-z0-9]+")
_ROOTS = "approved_roots"


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    path: str
    remote_url: str | None = None  # manual remote; wins over the detected one
    profile: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if any(ord(c) < 32 for c in v):
            raise ValueError("name contains control characters")
        return v.strip()


class RemoteIn(BaseModel):
    remote_url: str | None


class TrustIn(BaseModel):
    trusted: bool


class IncludeIn(BaseModel):
    included: bool


class SourceIn(BaseModel):
    pattern: str = Field(min_length=1, max_length=300)
    kind: str = Field(pattern="^(instruction|task|memory|other)$")


def _slug(name: str) -> str:
    return _SLUG.sub("-", name.lower()).strip("-")[:64] or "project"


def _profile_for(name: str | None) -> Profile | None:
    if not name:
        return None
    try:
        return load_profile(name)
    except ProfileError as exc:
        raise HTTPException(422, str(exc)) from exc


def _event(s: Session, project_id: int | None, kind: str, **detail: object) -> None:
    s.add(Event(project_id=project_id, kind=kind, detail=json.dumps(detail)))


def _extra_patterns(engine: Engine, project: Project) -> dict[str, tuple[str, ...]]:
    """Profile globs plus user-added globs (stored in settings, key `project.<id>.patterns`)."""
    extra: dict[str, list[str]] = {"instruction": [], "memory": [], "task": [], "other": []}
    if project.profile:
        try:
            prof = load_profile(project.profile)
            extra["instruction"] += prof.instruction_sources
            extra["memory"] += prof.memory_sources
            extra["task"] += prof.task_sources
        except ProfileError:
            pass
    for item in get_setting(engine, f"project.{project.id}.patterns", []):
        extra[item["kind"]].append(item["pattern"])
    return {k: tuple(v) for k, v in extra.items()}


def _refresh(engine: Engine, project: Project) -> None:
    """Re-scan sources: upsert rows, keep the include flag, forget files that disappeared."""
    root = Path(project.path)
    extra = _extra_patterns(engine, project)
    try:
        found = scan.scan(
            root,
            instruction=extra["instruction"],
            memory=extra["memory"],
            task=extra["task"],
            other=extra["other"],
        )
    except PathPolicyError as exc:
        raise HTTPException(409, f"project folder is no longer usable: {exc}") from exc
    now = datetime.now(UTC)
    seen = {f.rel_path for f in found}
    with Session(engine) as s:
        rows = {
            r.rel_path: r
            for r in s.scalars(select(ProjectSource).where(ProjectSource.project_id == project.id))
        }
        for f in found:
            row = rows.get(f.rel_path)
            if row is None:
                s.add(
                    ProjectSource(
                        project_id=project.id,
                        kind=f.kind,
                        rel_path=f.rel_path,
                        content_hash=f.content_hash,
                        last_seen_at=now,
                    )
                )
            else:
                row.kind, row.content_hash, row.last_seen_at = f.kind, f.content_hash, now
        for rel, row in rows.items():
            if rel not in seen:
                s.delete(row)
        _event(s, project.id, "project.refreshed", sources=len(found))
        s.commit()


def _view(engine: Engine, p: Project, *, detail: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": p.id,
        "slug": p.slug,
        "name": p.name,
        "path": p.path,
        "remote_url": p.remote_url,
        "needs_remote": p.remote_url is None,
        "integration_mode": p.integration_mode,
        "workspace_trusted": p.workspace_trusted,
        "profile": p.profile,
        "policy": asdict(worker_policy(p.workspace_trusted)),
    }
    if not detail:
        return out
    root = Path(p.path)
    with Session(engine) as s:
        sources = list(
            s.scalars(
                select(ProjectSource)
                .where(ProjectSource.project_id == p.id)
                .order_by(ProjectSource.kind, ProjectSource.rel_path)
            )
        )
    try:
        git = asdict(gitinfo.inspect(root))
    except OSError:
        git = {"is_repo": False, "error": "project folder unreadable"}
    out["git"] = git
    out["sources"] = [
        {
            "id": r.id,
            "kind": r.kind,
            "rel_path": r.rel_path,
            "included": r.included,
            "content_hash": r.content_hash,
            "requires_trust": scan.requires_trust(r.rel_path),
            "active": r.included and (p.workspace_trusted or not scan.requires_trust(r.rel_path)),
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        }
        for r in sources
    ]
    out["commands"] = [
        {"purpose": c.purpose, "argv": list(c.argv), "source": c.source}
        for c in detect_commands(root)
    ]
    out["unity_version"] = detect_unity(root)
    return out


def _get(engine: Engine, project_id: int) -> Project:
    with Session(engine) as s:
        p = s.get(Project, project_id)
        if p is None:
            raise HTTPException(404, "unknown project")
        s.expunge(p)
        return p


@router.get("/profiles")
def profiles() -> dict[str, list[dict[str, Any]]]:
    out = []
    for name in list_profiles():
        try:
            prof = load_profile(name)
        except ProfileError:
            continue
        out.append({"id": name, "suggested_path": prof.path, "remote": prof.remote})
    return {"profiles": out}


@router.get("/projects")
def list_projects(request: Request) -> dict[str, list[dict[str, Any]]]:
    engine = request.app.state.engine
    with Session(engine) as s:
        projects = list(s.scalars(select(Project).order_by(Project.name)))
        s.expunge_all()
    return {"projects": [_view(engine, p, detail=False) for p in projects]}


@router.post("/projects", status_code=201)
def add_project(body: ProjectIn, request: Request) -> dict[str, Any]:
    engine: Engine = request.app.state.engine
    prof = _profile_for(body.profile)
    try:
        root = validate_root(body.path)
    except PathPolicyError as exc:
        raise HTTPException(422, f"path rejected: {exc}") from exc
    data_dir = request.app.state.settings.data_dir.resolve()
    if root == data_dir or data_dir in root.parents or root in data_dir.parents:
        raise HTTPException(422, "path must not overlap the CPM data directory")
    remote = None
    if body.remote_url:
        try:
            remote = gitinfo.validate_remote_url(body.remote_url)
        except gitinfo.RemoteUrlError as exc:
            raise HTTPException(422, str(exc)) from exc
    elif gitinfo.is_repo_root(root):
        try:
            remote = gitinfo.detect_remote(root)
        except gitinfo.GitUnavailableError:
            remote = None
    if remote is None and prof and prof.remote:
        remote = gitinfo.validate_remote_url(prof.remote)

    with Session(engine) as s:
        key = os.path.normcase(str(root))
        for existing in s.scalars(select(Project)):
            if os.path.normcase(existing.path) == key:
                raise HTTPException(409, "this folder is already registered")
        base, slug, n = _slug(body.name), "", 1
        taken = set(s.scalars(select(Project.slug)))
        slug = base
        while slug in taken:
            n += 1
            suffix = f"-{n}"
            slug = base[: 64 - len(suffix)] + suffix
        project = Project(
            slug=slug,
            name=body.name,
            path=str(root),
            remote_url=remote,
            integration_mode=prof.integration if prof else "pr_only",
            workspace_trusted=False,
            profile=prof.id if prof else None,
        )
        s.add(project)
        s.flush()
        _event(s, project.id, "project.added", slug=slug, profile=project.profile)
        s.commit()
        s.refresh(project)
        s.expunge(project)
    roots = get_setting(engine, _ROOTS, [])
    if str(root) not in roots:
        set_setting(engine, _ROOTS, [*roots, str(root)])
    _refresh(engine, project)
    return _view(engine, project, detail=True)


@router.get("/projects/{project_id}")
def get_project(project_id: int, request: Request) -> dict[str, Any]:
    engine = request.app.state.engine
    return _view(engine, _get(engine, project_id), detail=True)


@router.post("/projects/{project_id}/refresh")
def refresh_project(project_id: int, request: Request) -> dict[str, Any]:
    engine = request.app.state.engine
    project = _get(engine, project_id)
    _refresh(engine, project)
    return _view(engine, project, detail=True)


@router.put("/projects/{project_id}/remote")
def set_remote(project_id: int, body: RemoteIn, request: Request) -> dict[str, Any]:
    engine = request.app.state.engine
    remote = None
    if body.remote_url:
        try:
            remote = gitinfo.validate_remote_url(body.remote_url)
        except gitinfo.RemoteUrlError as exc:
            raise HTTPException(422, str(exc)) from exc
    with Session(engine) as s:
        p = s.get(Project, project_id)
        if p is None:
            raise HTTPException(404, "unknown project")
        p.remote_url = remote
        _event(s, p.id, "project.remote_set", remote=remote)
        s.commit()
    return _view(engine, _get(engine, project_id), detail=True)


@router.put("/projects/{project_id}/trust")
def set_trust(project_id: int, body: TrustIn, request: Request) -> dict[str, Any]:
    engine = request.app.state.engine
    with Session(engine) as s:
        p = s.get(Project, project_id)
        if p is None:
            raise HTTPException(404, "unknown project")
        p.workspace_trusted = body.trusted
        _event(s, p.id, "project.trust_changed", trusted=body.trusted)
        s.commit()
    return _view(engine, _get(engine, project_id), detail=True)


@router.put("/projects/{project_id}/sources/{source_id}")
def set_included(
    project_id: int, source_id: int, body: IncludeIn, request: Request
) -> dict[str, Any]:
    engine = request.app.state.engine
    with Session(engine) as s:
        row = s.get(ProjectSource, source_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(404, "unknown source")
        row.included = body.included
        _event(
            s, project_id, "project.source_toggled", rel_path=row.rel_path, included=body.included
        )
        s.commit()
    return _view(engine, _get(engine, project_id), detail=True)


@router.post("/projects/{project_id}/patterns")
def add_pattern(project_id: int, body: SourceIn, request: Request) -> dict[str, Any]:
    """Add a glob (relative, inside the project) that the next refresh scans for sources."""
    engine = request.app.state.engine
    project = _get(engine, project_id)
    try:
        pattern = scan.check_pattern(body.pattern)
    except PathPolicyError as exc:
        raise HTTPException(422, str(exc)) from exc
    key = f"project.{project_id}.patterns"
    items = get_setting(engine, key, [])
    item = {"kind": body.kind, "pattern": pattern}
    if item not in items:
        set_setting(engine, key, [*items, item])
    _refresh(engine, project)
    return _view(engine, project, detail=True)


@router.delete("/projects/{project_id}", status_code=204)
def remove_project(project_id: int, request: Request) -> None:
    """Unregister only. Nothing on disk is touched."""
    engine = request.app.state.engine
    with Session(engine) as s:
        p = s.get(Project, project_id)
        if p is None:
            raise HTTPException(404, "unknown project")
        _event(s, None, "project.removed", slug=p.slug)
        s.delete(p)
        # ids can be reused by SQLite; stale per-project settings must not leak to the next one
        stale = s.get(Setting, f"project.{project_id}.patterns")
        if stale is not None:
            s.delete(stale)
        s.commit()
