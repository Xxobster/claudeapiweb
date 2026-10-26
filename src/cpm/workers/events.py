"""Run event log: one redacted JSONL file per run (docs/THREATS.md T-01, T-11).

The supervisor is the only writer. The WebSocket stream and the REST endpoint read the same file,
so a reconnecting browser can replay from any `seq`. Every string is redacted and truncated before
it is written; the file is size-capped so a chatty or hostile worker cannot fill the disk.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cpm.security.redaction import redact

MAX_FIELD_CHARS = 4000
MAX_LOG_BYTES = 20_000_000
MAX_READ = 1000


def _clean(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        text = redact(value)
        return text if len(text) <= MAX_FIELD_CHARS else text[:MAX_FIELD_CHARS] + "...[truncated]"
    if depth > 6:
        return "[nested too deep]"
    if isinstance(value, dict):
        return {str(k)[:100]: _clean(v, depth + 1) for k, v in list(value.items())[:100]}
    if isinstance(value, list | tuple):
        return [_clean(v, depth + 1) for v in list(value)[:100]]
    if value is None or isinstance(value, bool | int | float):
        return value
    return _clean(str(value), depth + 1)


class RunLog:
    """Append-only JSONL log. Thread-safe; `seq` starts at 1 and survives a supervisor restart."""

    def __init__(self, path: Path, max_bytes: int = MAX_LOG_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._full = False
        path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = sum(1 for _ in path.open("rb")) if path.exists() else 0

    def append(self, type_: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            ev: dict[str, Any] = {
                "seq": self._seq,
                "ts": datetime.now(UTC).isoformat(),
                "type": type_,
                **{k: _clean(v) for k, v in fields.items() if k not in ("seq", "ts", "type")},
            }
            if self._full:
                return ev
            line = json.dumps(ev, ensure_ascii=False) + "\n"
            if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                self._full = True
                line = json.dumps(
                    {"seq": ev["seq"], "ts": ev["ts"], "type": "log_full",
                     "note": "event log size cap reached; later events are dropped"}
                ) + "\n"  # fmt: skip
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            return ev

    def read(self, since: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """Events with seq > since (at most `limit`, never more than MAX_READ)."""
        limit = max(1, min(limit, MAX_READ))
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for n, line in enumerate(fh):
                if n < since:
                    continue  # one line per seq (1-based): skip replayed lines without parsing
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue  # a partially written last line
                if ev.get("seq", 0) > since:
                    out.append(ev)
                    if len(out) >= limit:
                        break
        return out
