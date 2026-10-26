"""Detect edits made outside CPM in a project's main tree (scenario 7, THREATS T-18).

Claude Desktop edits `D:\\projects\\game` directly and cannot be forced to respect reservations.
A scan takes a cheap git snapshot (HEAD + `git status` entries with size/mtime), compares it with
the stored previous one and reports which active reservations overlap the changed paths. Polling
git replaces a filesystem watcher (no extra dependency, works on junction-heavy Windows trees);
the scheduler calls `scan` periodically and WP10 repeats it before any integration.

Nothing in the main tree is ever written or reverted. A hit only records an event and returns the
affected tasks; the decision (pause, replan, merge-tree revalidation) belongs to scheduler / gate.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, insert, select

from cpm.db.models import Event, Project, Reservation, utcnow
from cpm.db.settings_repo import get_setting, set_setting
from cpm.discovery import gitinfo
from cpm.locks import normalize
from cpm.security.redaction import redact

MAX_PATHS = 5000
_SHA = re.compile(r"^[0-9a-f]{40}$")

Entry = tuple[str, int, int]  # (porcelain status, size, mtime_ns); size -1 = missing


@dataclass
class Snapshot:
    head: str
    entries: dict[str, Entry]
    truncated: bool = False

    def dump(self) -> dict[str, Any]:
        return {
            "head": self.head, "truncated": self.truncated,
            "entries": {k: list(v) for k, v in self.entries.items()},
        }  # fmt: skip

    @staticmethod
    def load(raw: Any) -> Snapshot | None:
        try:
            ents = {str(k): (str(v[0]), int(v[1]), int(v[2])) for k, v in raw["entries"].items()}
            return Snapshot(str(raw["head"]), ents, bool(raw.get("truncated", False)))
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return None


@dataclass
class ExternalConflict:
    task_id: int
    paths: list[str]


@dataclass
class ScanResult:
    baseline: bool
    changed: list[str] = field(default_factory=list)
    conflicts: list[ExternalConflict] = field(default_factory=list)
    truncated: bool = False


def take_snapshot(root: Path) -> Snapshot:
    """HEAD and the dirty entries of the main tree, relative to `root`. Read-only."""
    try:
        head = gitinfo._git(root, "rev-parse", "HEAD").strip()
    except gitinfo.GitUnavailableError:
        head = ""  # empty repository
    prefix = gitinfo._git(root, "rev-parse", "--show-prefix").strip()
    # `git status` runs repo-defined clean/smudge filters; blank them like gitinfo.inspect does.
    out = gitinfo._git(
        root,
        "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
        config=gitinfo._filter_overrides(root),
    )  # fmt: skip
    entries: dict[str, Entry] = {}
    truncated = False
    for rec in out.split("\0"):
        if len(rec) < 4:
            continue
        status, path = rec[:2], rec[3:]
        if prefix:
            if not path.startswith(prefix):
                continue
            path = path[len(prefix) :]
        if len(entries) >= MAX_PATHS:
            truncated = True
            break
        try:
            st = os.lstat(root / path)
            entries[path] = (status, st.st_size, st.st_mtime_ns)
        except OSError:
            entries[path] = (status, -1, 0)
    return Snapshot(head if _SHA.match(head) else "", entries, truncated)


def changed_paths(root: Path, old: Snapshot, new: Snapshot) -> list[str]:
    changed = {
        p
        for p in old.entries.keys() | new.entries.keys()
        if old.entries.get(p) != new.entries.get(p)
    }
    if old.head and new.head and old.head != new.head:
        try:
            names = gitinfo._git(root, "diff", "--name-only", "-z", old.head, new.head)
            changed.update(n for n in names.split("\0") if n)
        except gitinfo.GitUnavailableError:
            changed.add("")  # HEAD moved and we cannot list how: treat the whole tree as changed
    elif old.head != new.head:
        changed.add("")
    return sorted(changed)[:MAX_PATHS]


class ExternalWatcher:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _key(project_id: int) -> str:
        return f"project.{project_id}.tree_snapshot"

    def scan(self, project_id: int) -> ScanResult:
        with self.engine.connect() as conn:
            root_s = conn.execute(select(Project.path).where(Project.id == project_id)).scalar()
        if root_s is None:
            raise LookupError("unknown project")
        root = Path(root_s)
        new = take_snapshot(root)
        old = Snapshot.load(get_setting(self.engine, self._key(project_id)))
        set_setting(self.engine, self._key(project_id), new.dump())
        if old is None:
            return ScanResult(baseline=True, truncated=new.truncated)
        changed = changed_paths(root, old, new)
        result = ScanResult(
            baseline=False, changed=changed, truncated=new.truncated or old.truncated
        )
        if not changed:
            return result
        whole = "" in changed
        keys = {p: normalize.normalize_git_path(p) for p in changed if p}
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(Reservation).where(
                    Reservation.project_id == project_id, Reservation.resource_type == "path"
                )
            ).all()
        hits: dict[int, list[str]] = {}
        for r in rows:
            if whole:
                hits.setdefault(r.task_id, []).append("(whole tree: HEAD moved)")
                continue
            for p, k in keys.items():
                if normalize.overlaps(r.resource_key, k):
                    hits.setdefault(r.task_id, []).append(p)
        result.conflicts = [
            ExternalConflict(t, sorted(set(ps))[:50]) for t, ps in sorted(hits.items())
        ]
        with self.engine.begin() as conn:
            conn.execute(
                insert(Event).values(
                    ts=utcnow(),
                    project_id=project_id,
                    kind="external.modification",
                    detail=redact(json.dumps({"count": len(changed), "sample": changed[:20]})),
                )
            )
            for c in result.conflicts:
                conn.execute(
                    insert(Event).values(
                        ts=utcnow(),
                        project_id=project_id,
                        task_id=c.task_id,
                        kind="external.conflict",
                        detail=redact(json.dumps({"paths": c.paths[:20], "total": len(c.paths)})),
                    )
                )
        return result
