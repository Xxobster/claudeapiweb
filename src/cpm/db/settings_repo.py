"""Key/value settings in SQLite. Never store secrets here (the table holds plain text)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from cpm.db.models import Setting


def get_setting(engine: Engine, key: str, default: Any = None) -> Any:
    with Session(engine) as s:
        row = s.get(Setting, key)
        return default if row is None else json.loads(row.value)


def set_setting(engine: Engine, key: str, value: Any) -> None:
    """Upsert in one statement: two concurrent first writes of a key must not hit the unique key."""
    body = json.dumps(value)
    with Session(engine) as s:
        s.execute(
            insert(Setting)
            .values(key=key, value=body)
            .on_conflict_do_update(index_elements=[Setting.key], set_={"value": body})
        )
        s.commit()
