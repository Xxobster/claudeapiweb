"""Deterministic TODO import: Markdown checkbox lists -> tasks (PLAN.md WP4, scenario 4).

Text read from a managed repository is untrusted data: it only ever becomes task titles and
descriptions, never instructions (CLAUDE.md rule 9, THREATS T-05). Worker prompts must keep
wrapping it as quoted data (WP5).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from cpm.tasks.service import MAX_TITLE, TaskData, TaskService

MAX_TEXT_BYTES = 256 * 1024
MAX_ITEMS = 500

_ITEM = re.compile(
    r"^(?P<indent>[ \t]*)(?:[-*+]|\d{1,3}[.)])[ \t]+\[(?P<mark>[ xX])\][ \t]+(?P<text>\S.*)$"
)
_HEADING = re.compile(r"^#{1,6}[ \t]+(?P<text>\S.*?)[ \t#]*$")


@dataclass(frozen=True)
class TodoItem:
    text: str
    heading: str
    depth: int
    parent: int | None  # index into the item list
    done: bool


def parse_todo(text: str) -> list[TodoItem]:
    if len(text.encode("utf-8", errors="replace")) > MAX_TEXT_BYTES:
        raise ValueError(f"TODO text larger than {MAX_TEXT_BYTES} bytes")
    items: list[TodoItem] = []
    heading = ""
    stack: list[tuple[int, int]] = []  # (indent width, item index)
    fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        h = _HEADING.match(line)
        if h:
            heading = h.group("text").strip()
            stack.clear()
            continue
        m = _ITEM.match(line)
        if not m:
            continue
        width = len(m.group("indent").replace("\t", "    "))
        while stack and stack[-1][0] >= width:
            stack.pop()
        parent = stack[-1][1] if stack else None
        idx = len(items)
        items.append(
            TodoItem(
                text="".join(c for c in m.group("text") if ord(c) >= 32 or c == "\t").strip(),
                heading=heading,
                depth=len(stack),
                parent=parent,
                done=m.group("mark") in "xX",
            )
        )
        stack.append((width, idx))
        if len(items) > MAX_ITEMS:
            raise ValueError(f"more than {MAX_ITEMS} TODO items")
    return items


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _refs(source: str, items: list[TodoItem]) -> list[str]:
    """Stable per-item references: heading + ancestor chain + text, plus an occurrence counter
    so identical siblings stay distinct while re-imports give the same references."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for it in items:
        chain: list[str] = []
        p = it.parent
        while p is not None:
            chain.append(_norm(items[p].text))
            p = items[p].parent
        key = "\n".join([source, it.heading, *reversed(chain), _norm(it.text)])
        n = seen.get(key, 0)
        seen[key] = n + 1
        out.append(hashlib.sha256(f"{key}\n#{n}".encode()).hexdigest()[:32])
    return out


@dataclass
class ImportResult:
    created: list[TaskData]
    skipped_done: int
    skipped_existing: int


def import_todo(
    svc: TaskService, project_id: int, text: str, source_name: str, *, actor: str
) -> ImportResult:
    """Create BACKLOG tasks for open checkbox items. Re-importing the same file adds nothing."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from cpm.db.models import Task

    items = parse_todo(text)
    refs = _refs(source_name, items)
    with Session(svc.engine) as s:
        existing = {
            r: i
            for r, i in s.execute(
                select(Task.source_ref, Task.id).where(
                    Task.project_id == project_id, Task.source_ref.is_not(None)
                )
            )
        }
    created: list[TaskData] = []
    ids: dict[int, int] = {}
    done = dup = 0
    for idx, it in enumerate(items):
        if it.done:
            done += 1
            continue
        ref = refs[idx]
        if ref in existing:
            ids[idx] = existing[ref]
            dup += 1
            continue
        parent = ids.get(it.parent) if it.parent is not None else None
        t = svc.create(
            project_id,
            it.text[:MAX_TITLE],
            description=(f"From {source_name}" + (f", section: {it.heading}" if it.heading else "")
                         + ("\n\n" + it.text if len(it.text) > MAX_TITLE else "")),
            parent_id=parent,
            source="todo",
            source_ref=ref,
        )  # fmt: skip
        ids[idx] = t.id
        existing[ref] = t.id
        created.append(t)
    return ImportResult(created, done, dup)
