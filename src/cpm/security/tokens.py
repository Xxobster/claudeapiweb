"""Install token and CSRF token (docs/THREATS.md T-02).

The install token is random, created once in the data directory and required on every API call
except the public allowlist. The CSRF token is derived from it, so it needs no extra storage.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cpm.security.redaction import register_secret

TOKEN_FILE = "install_token"  # noqa: S105 (file name, not a secret)


def load_or_create_token(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / TOKEN_FILE
    if path.exists():
        try:
            value = path.read_text(encoding="ascii").strip()
        except UnicodeDecodeError:  # corrupt file: fall through and create a fresh token
            value = ""
        if len(value) >= 32:
            register_secret(value)
            return value
    value = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(value + "\n")
    with contextlib.suppress(OSError):  # Windows: ACL inherited from the private data dir
        os.chmod(path, 0o600)
    register_secret(value)
    return value


def csrf_for(token: str) -> str:
    value = hmac.new(token.encode(), b"cpm-csrf-v1", hashlib.sha256).hexdigest()
    register_secret(value)
    return value


def same(a: str | None, b: str) -> bool:
    return a is not None and hmac.compare_digest(a.encode(), b.encode())
