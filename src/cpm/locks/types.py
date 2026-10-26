"""Value types shared by reservations, claims, adapters and admission."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from cpm.locks import normalize

Mode = Literal["excl", "shared"]
HEAVY_DEFAULT = frozenset({"unity_editor", "gpu", "blender", "device"})
MAX_REQUESTS = 200


@dataclass(frozen=True)
class ResourceRequest:
    type: str  # "path" or one of normalize.NAMED_TYPES
    key: str  # canonical key (never raw user text)
    mode: Mode = "excl"
    label: str = ""

    @property
    def per_project(self) -> bool:
        return self.type in normalize.PER_PROJECT_TYPES


@dataclass(frozen=True)
class Blocker:
    kind: str  # reservation | desktop_claim | job_board | lock_file | heavy
    resource: str
    message: str
    holder_task_id: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "resource": self.resource,
            "message": self.message,
            "holder_task_id": self.holder_task_id,
        }


class ReservationConflict(Exception):
    """The requested set cannot be granted as a whole; nothing was reserved."""

    def __init__(self, blockers: list[Blocker]) -> None:
        super().__init__("; ".join(b.message for b in blockers) or "conflict")
        self.blockers = blockers


@dataclass
class Grant:
    task_id: int
    resources: list[ResourceRequest] = field(default_factory=list)
    lease_seconds: int = 0


@dataclass(frozen=True)
class ReservationInfo:
    id: int
    task_id: int
    project_id: int | None
    type: str
    key: str
    label: str
    mode: str
    heartbeat_at: str
    lease_expires_at: str
