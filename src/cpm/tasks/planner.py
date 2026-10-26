"""AI planner seam. WP4 ships only the deterministic mock; a live planner (Haiku, medium) is added
with the providers in WP5 and needs Micah's budget go (CLAUDE.md rule 7). Dependency tracking,
ready-queue selection and transitions never use the planner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from cpm.tasks.service import MAX_TITLE, TaskError

MAX_PLANNED = 50


@dataclass(frozen=True)
class PlannedTask:
    title: str
    description: str = ""


class Planner(Protocol):
    name: str

    def plan(self, prompt: str) -> list[PlannedTask]: ...


class PlannerUnavailable(TaskError):
    status = 501


class MockPlanner:
    """One task per non-empty line (bullet/number markers stripped). Same input, same output."""

    name = "mock"

    def plan(self, prompt: str) -> list[PlannedTask]:
        out: list[PlannedTask] = []
        for line in prompt.splitlines():
            text = re.sub(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+(?:\[[ xX]\]\s+)?", "", line).strip()
            if text:
                out.append(
                    PlannedTask(
                        title=text[:MAX_TITLE], description=text if len(text) > MAX_TITLE else ""
                    )
                )
            if len(out) > MAX_PLANNED:
                raise ValueError(f"more than {MAX_PLANNED} planned tasks")
        return out


def get_planner(live: bool = False) -> Planner:
    if live:
        raise PlannerUnavailable("the live AI planner arrives with the worker providers (WP5)")
    return MockPlanner()
