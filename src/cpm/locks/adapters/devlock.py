"""Device locks (`tools/devices/devlock.py`, Nirandyr rule 24).

CPM does not execute the managed repo's `devlock.py` itself (that would run repo code inside the
supervisor). The adapter

* lists the lock files in `%USERPROFILE%/.device-locks` for display (names and age only, the
  file format is not interpreted), and
* builds the exact argument list a worker must use to take a device: `devlock.py run ...`, which
  does the real exclusion and queueing. Lanes (WP14) run device commands only through it.
Device exclusion inside CPM itself is the `device:<serial>` reservation.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from cpm.locks.types import Blocker, ResourceRequest

_SERIAL = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")
_WHO = re.compile(r"^[A-Za-z0-9 ._:\-]{1,64}$")
MAX_LISTED = 50


def default_lock_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".device-locks"


def wrap_command(
    script: str | os.PathLike[str],
    *,
    device: str,
    who: str,
    job: str,
    prio: int,
    argv: list[str],
    python: str | None = None,
) -> list[str]:
    """`python devlock.py run --device D --who W --job J --prio P -- <argv>` (rule 24)."""
    if not _SERIAL.match(device) or not _WHO.match(who) or not _WHO.match(job):
        raise ValueError("invalid device, who or job")
    if not 1 <= prio <= 4:
        raise ValueError("prio must be 1-4")
    if not argv or any(not isinstance(a, str) or "\x00" in a for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    return [
        python or sys.executable, "-I", os.fspath(script), "run",
        "--device", device, "--who", who, "--job", job, "--prio", str(prio), "--", *argv,
    ]  # fmt: skip


class DevLock:
    name = "devlock"

    def __init__(self, lock_dir: str | os.PathLike[str] | None = None) -> None:
        self.dir = Path(lock_dir) if lock_dir else default_lock_dir()

    def blockers(self, requests: list[ResourceRequest], *, heavy: bool) -> list[Blocker]:
        return []  # informational: devlock.py itself arbitrates when the worker runs

    def status(self) -> dict[str, object]:
        held: list[dict[str, object]] = []
        try:
            if self.dir.is_dir():
                for p in sorted(self.dir.iterdir())[:MAX_LISTED]:
                    if p.is_file():
                        held.append(
                            {"file": p.name, "age_s": max(0, int(time.time() - p.stat().st_mtime))}
                        )
        except OSError:
            pass
        return {"name": self.name, "kind": "devlock", "active": bool(held), "held": held}
