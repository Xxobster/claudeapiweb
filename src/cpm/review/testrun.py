"""Run a project's test commands in a task worktree (WP10; docs/THREATS.md T-30).

Test commands execute repository code, so the gate only calls this for a TRUSTED workspace.
Commands are argument lists (profile `test_commands` or `discovery.commands`), never shell strings;
the child gets the same credential-free environment as a mock worker and is killed as a tree on
timeout. Output is capped to a redacted tail.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cpm.security.childenv import build_mock_env
from cpm.security.redaction import redact
from cpm.workers import proctree

TAIL_BYTES = 16_000
DEFAULT_TIMEOUT_S = 900.0


@dataclass
class TestOutcome:
    __test__ = False  # not a pytest class
    argv: list[str]
    exit_code: int | None  # None = timed out / could not start
    timed_out: bool
    duration_s: float
    tail: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class TestRunner(Protocol):
    __test__: bool

    def run(self, argv: list[str], cwd: Path, timeout_s: float) -> TestOutcome: ...


def _drain(stream: object, sink: deque[bytes], size: list[int]) -> None:
    read = stream.read1 if hasattr(stream, "read1") else stream.read  # type: ignore[attr-defined]
    while chunk := read(8192):
        sink.append(chunk)
        size[0] += len(chunk)
        while size[0] > TAIL_BYTES * 2 and len(sink) > 1:
            size[0] -= len(sink.popleft())


class SubprocessTestRunner:
    __test__ = False

    def run(self, argv: list[str], cwd: Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> TestOutcome:
        t0 = time.monotonic()
        try:
            tree = proctree.spawn(argv, build_mock_env(None, {"PYTHONIOENCODING": "utf-8"}), cwd)
        except OSError as exc:
            return TestOutcome(argv, None, False, 0.0, f"could not start: {type(exc).__name__}")
        sink: deque[bytes] = deque()
        size = [0]
        readers = [
            threading.Thread(target=_drain, args=(s, sink, size), daemon=True)
            for s in (tree.popen.stdout, tree.popen.stderr)
            if s is not None
        ]
        for r in readers:
            r.start()
        timed_out = False
        try:
            tree.popen.wait(timeout=timeout_s)
        except Exception:  # subprocess.TimeoutExpired
            timed_out = True
        finally:
            tree.kill()  # also reaps grandchildren a finished test run left behind
            tree.popen.wait()
            for r in readers:
                r.join(2.0)
            tree.close()
        text = b"".join(sink)[-TAIL_BYTES:].decode("utf-8", "replace")
        return TestOutcome(
            argv,
            None if timed_out else tree.popen.returncode,
            timed_out,
            round(time.monotonic() - t0, 2),
            redact(text),
        )
