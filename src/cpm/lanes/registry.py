"""Plugin points for lane types (WP13 voice, WP14 3D/map/Unity).

A validator is a function `(ValidationContext, params) -> list[Finding]` registered under a
`kind` name that profiles reference. Registration happens at import time of the lane module.
A validator must be read-only: it inspects files in the task worktree and may run nothing
heavy. If it cannot judge (a tool is missing on this PC) it raises `Unavailable`, which makes the
gate wait for a person instead of passing or failing the task.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Severity = Literal["error", "warn"]


@dataclass(frozen=True)
class ChangedFile:
    path: str  # worktree-relative, '/' separated
    status: str  # A | M | D | T
    size: int
    mode: str = "100644"


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    path: str | None = None
    severity: Severity = "error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "message": self.message, "path": self.path,
            "severity": self.severity,
        }  # fmt: skip


@dataclass
class ValidationContext:
    worktree: Path
    changed: list[ChangedFile]  # D entries included; most validators want `present`
    lane_id: str
    asset_paths: list[str] = field(default_factory=list)

    @property
    def present(self) -> list[ChangedFile]:
        return [c for c in self.changed if c.status != "D"]


class Unavailable(Exception):
    """The validator cannot run here (missing tool/PC); a person must look."""


Validator = Callable[[ValidationContext, dict[str, Any]], list[Finding]]
_VALIDATORS: dict[str, Validator] = {}


def register_validator(kind: str, fn: Validator) -> None:
    if kind in _VALIDATORS and _VALIDATORS[kind] is not fn:
        raise ValueError(f"validator {kind!r} is already registered")
    _VALIDATORS[kind] = fn


def get_validator(kind: str) -> Validator | None:
    return _VALIDATORS.get(kind)


def validator_kinds() -> list[str]:
    return sorted(_VALIDATORS)
