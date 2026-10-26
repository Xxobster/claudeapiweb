"""`BUILD_RUNNING.lock`, `BLENDER_RUNNING.lock` and the gpu-lock folder (Nirandyr rules 26, 27, 49).

Heavy requests are refused while any of these exists. The contents are never read (only
existence, count and age), so a hostile file cannot inject text into messages.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from cpm.locks.types import Blocker, ResourceRequest

MAX_LISTED = 50


def _age_s(path: Path) -> int | None:
    try:
        return max(0, int(time.time() - path.stat().st_mtime))
    except OSError:
        return None


class LockFile:
    def __init__(self, name: str, path: str | os.PathLike[str]) -> None:
        self.name = name
        self.path = Path(path)

    def _present(self) -> bool:
        try:
            return self.path.is_file()
        except OSError:
            return True  # cannot tell: fail closed

    def blockers(self, requests: list[ResourceRequest], *, heavy: bool) -> list[Blocker]:
        if heavy and self._present():
            return [
                Blocker(
                    "lock_file", self.name,
                    f"blocked by: `{self.path.name}` exists (no heavy work while it does)",
                )
            ]  # fmt: skip
        return []

    def status(self) -> dict[str, object]:
        present = self._present()
        return {
            "name": self.name, "kind": "lock_file", "file": self.path.name,
            "active": present, "age_s": _age_s(self.path) if present else None,
        }  # fmt: skip


class LockDir:
    """Any `*.lock` file directly inside the folder counts as an active lock."""

    def __init__(self, name: str, path: str | os.PathLike[str]) -> None:
        self.name = name
        self.path = Path(path)

    def _locks(self) -> tuple[list[str], bool]:
        try:
            if not self.path.is_dir():
                return [], False
            names = sorted(p.name for p in self.path.iterdir() if p.suffix == ".lock")
            return names, False
        except OSError:
            return [], True  # unreadable: fail closed

    def blockers(self, requests: list[ResourceRequest], *, heavy: bool) -> list[Blocker]:
        names, unreadable = self._locks()
        if heavy and (names or unreadable):
            why = "cannot be read" if unreadable else f"holds {len(names)} lock file(s)"
            return [Blocker("lock_file", self.name, f"blocked by: GPU lock folder {why}")]
        return []

    def status(self) -> dict[str, object]:
        names, unreadable = self._locks()
        return {
            "name": self.name, "kind": "lock_dir", "active": bool(names) or unreadable,
            "files": names[:MAX_LISTED], "unreadable": unreadable,
        }  # fmt: skip
