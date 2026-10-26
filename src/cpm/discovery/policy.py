"""Workspace-trust policy handed to the worker runner (WP5).

Until the user trusts a workspace, hooks, MCP servers, skills and project settings files are off
and no project setting source is loaded. `user` settings are never loaded for workers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerPolicy:
    setting_sources: tuple[str, ...]
    allow_hooks: bool
    allow_mcp: bool
    allow_project_scripts: bool


def worker_policy(trusted: bool) -> WorkerPolicy:
    if trusted:
        return WorkerPolicy(("project",), True, True, True)
    return WorkerPolicy((), False, False, False)
