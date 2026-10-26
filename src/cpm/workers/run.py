"""Child process entry point: `python -I -m cpm.workers.run --spec <file>`.

Protocol: one JSON object per stdout line (`{"type": ..., ...}`); the supervisor reads them and
owns the database. A line `stop` on stdin asks for a graceful stop. The child never opens the
database or the secret store. Started with `-I` and a neutral cwd so files in the (untrusted)
worktree can never shadow a module (the worktree is only handed over as a path).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from cpm.workers.providers import RunSpec, make_provider

_out_lock = threading.Lock()


def emit(type_: str, **fields: Any) -> None:
    line = json.dumps({"type": type_, **fields}, ensure_ascii=False, default=str)
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _watch_stdin(stop: threading.Event) -> None:
    for line in sys.stdin:
        if line.strip() == "stop":
            stop.set()
            return
    stop.set()  # the supervisor went away: stop instead of running unattended


def _heartbeat(stop: threading.Event, every: float) -> None:
    while not stop.wait(every):
        emit("heartbeat")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    args = ap.parse_args(argv)
    raw = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    spec = RunSpec(**raw)
    stop = threading.Event()
    threading.Thread(target=_watch_stdin, args=(stop,), daemon=True).start()
    emit("start", pid=os.getpid(), provider=spec.provider, model=spec.model)
    if spec.scenario != "hang":  # the hang scenario must stay silent
        threading.Thread(target=_heartbeat, args=(stop, spec.heartbeat_s), daemon=True).start()
    started = time.monotonic()
    try:
        code = int(make_provider(spec, emit, stop).run())
    except Exception as exc:  # noqa: BLE001 - last line of defence: report, never traceback with data
        emit("error", message=f"{type(exc).__name__}: {exc}")
        code = 1
    emit("exit", code=code, seconds=round(time.monotonic() - started, 2))
    return code


if __name__ == "__main__":
    sys.exit(main())
