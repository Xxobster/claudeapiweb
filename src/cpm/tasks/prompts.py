"""Prompt manager: named, append-only versioned prompt bodies (tables `prompts`)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from cpm.db.models import Prompt
from cpm.security.redaction import REDACTED, redact
from cpm.tasks.service import InvalidTask, NotFound

MAX_BODY = 50_000
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\- ]{0,127}$")


@dataclass
class PromptData:
    name: str
    version: int
    body: str


class PromptStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def save(self, name: str, body: str) -> PromptData:
        """Store a new version. Bodies that contain a secret are refused (rule 6, T-11)."""
        if not _NAME.match(name):
            raise InvalidTask("prompt name: 1-128 letters, digits, space, dot, dash, underscore")
        if not body.strip() or len(body) > MAX_BODY:
            raise InvalidTask(f"prompt body must be 1-{MAX_BODY} characters")
        if REDACTED in redact(body):
            raise InvalidTask("prompt looks like it contains a secret (key/token); not stored")
        with Session(self.engine) as s:
            latest = s.scalar(select(func.max(Prompt.version)).where(Prompt.name == name)) or 0
            p = Prompt(name=name, version=latest + 1, body=body)
            s.add(p)
            s.commit()
            return PromptData(p.name, p.version, p.body)

    def get(self, name: str, version: int | None = None) -> PromptData:
        with Session(self.engine) as s:
            q = select(Prompt).where(Prompt.name == name)
            q = q.where(Prompt.version == version) if version else q.order_by(Prompt.version.desc())
            p = s.scalars(q).first()
            if p is None:
                raise NotFound(f"prompt {name!r} not found")
            return PromptData(p.name, p.version, p.body)

    def list_latest(self) -> list[PromptData]:
        with Session(self.engine) as s:
            newest = (
                select(Prompt.name, func.max(Prompt.version).label("v"))
                .group_by(Prompt.name)
                .subquery()
            )
            rows = s.scalars(
                select(Prompt)
                .join(newest, (Prompt.name == newest.c.name) & (Prompt.version == newest.c.v))
                .order_by(Prompt.name)
            )
            return [PromptData(p.name, p.version, p.body) for p in rows]
