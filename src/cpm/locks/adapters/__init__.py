"""External lock adapters: read-only bridges to the managed project's own locking (WP6).

They never create, delete or modify a lock of the managed project, and never execute a script
from it. External programs cannot be forced to respect CPM's reservations, so these adapters only
make CPM refuse to start work that would collide (PLAN.md section 3, rule 4).
"""

from __future__ import annotations

from typing import Protocol

from cpm.locks.types import Blocker, ResourceRequest


class ExternalLock(Protocol):
    name: str

    def blockers(self, requests: list[ResourceRequest], *, heavy: bool) -> list[Blocker]: ...

    def status(self) -> dict[str, object]: ...
