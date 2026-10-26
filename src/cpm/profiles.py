"""Per-project profiles (`profiles/<name>.yaml`). Untrusted-input rules: safe_load, strict schema.

A profile carries machine-specific hints (suggested path, remote, lock locations). The project path
a user registers always wins; the profile never grants access to a folder by itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

PROFILE_DIR = Path(__file__).resolve().parents[2] / "profiles"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ProfileError(ValueError):
    pass


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    path: str | None = None
    remote: str | None = None
    worktree_root: str | None = None
    integration: str = Field(default="pr_only", pattern="^pr_only$")
    protected_branches: list[str] = ["main"]
    branch_pattern: str = "claude/{role}-{task_slug}"
    instruction_sources: list[str] = []
    memory_sources: list[str] = []
    task_sources: list[str] = []
    external_locks: dict[str, str] = {}
    # name -> absolute file whose existence means "heavy work must not start" (BUILD_RUNNING.lock)
    lock_files: dict[str, str] = {}
    # name -> absolute folder; any `*.lock` file inside means the same (gpu_lock.sh files)
    lock_dirs: dict[str, str] = {}
    heavy_resources: list[str] = []
    # Test commands the acceptance gate runs in the task worktree (argument lists, never strings).
    # Empty = use what `discovery.commands` detects. Only run for a trusted workspace.
    test_commands: list[list[str]] = []
    default_effort: str = "medium"
    pr_body_must_list: list[str] = []


def list_profiles() -> list[str]:
    if not PROFILE_DIR.is_dir():
        return []
    return sorted(p.stem for p in PROFILE_DIR.glob("*.yaml") if _NAME.match(p.stem))


def load_profile(name: str) -> Profile:
    if not _NAME.match(name):
        raise ProfileError("invalid profile name")
    path = PROFILE_DIR / f"{name}.yaml"
    if not path.is_file():
        raise ProfileError(f"unknown profile {name!r}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Profile.model_validate(data)
    except (yaml.YAMLError, ValueError) as exc:
        raise ProfileError(f"profile {name!r} is invalid") from exc
