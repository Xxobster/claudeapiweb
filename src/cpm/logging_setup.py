"""Structured-ish logging with secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from cpm.security.redaction import RedactingFormatter

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        if getattr(h, "_cpm", False):
            root.removeHandler(h)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_dir / "cpm.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
            )
        )
    for h in handlers:
        h.setFormatter(RedactingFormatter(_FORMAT))
        h._cpm = True  # type: ignore[attr-defined]
        root.addHandler(h)
