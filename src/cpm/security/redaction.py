"""Secret redaction for logs (docs/THREATS.md T-01, T-11).

Redaction happens in the Formatter, so it covers the message, its arguments and tracebacks.
"""

from __future__ import annotations

import logging
import re
import threading

REDACTED = "[REDACTED]"

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(
        r"(?i)((?:api[_-]?key|auth[_-]?token|authorization|password|secret|x-cpm-token)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]{4,}"
    ),
]

_lock = threading.Lock()
_known: set[str] = set()


def register_secret(value: str) -> None:
    """Make an exact secret value (e.g. the install token) redacted everywhere."""
    if len(value) >= 6:
        with _lock:
            _known.add(value)


def redact(text: str) -> str:
    with _lock:
        known = sorted(_known, key=len, reverse=True)
    for secret in known:
        text = text.replace(secret, REDACTED)
    for pat in _PATTERNS:
        if pat.groups:
            text = pat.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pat.sub(REDACTED, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
