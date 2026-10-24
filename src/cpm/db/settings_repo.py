"""Key/value settings in SQLite. Never store secrets here (the table holds plain text)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from cpm.db.models import Setting


def get_setting(engine: Engine, key: str, default: Any = None) -> Any:
    with Session(engine) as s:
        row = s.get(Setting, key)
        return default if row is None else json.loads(row.value)


def set_setting(engine: Engine, key: str, value: Any) -> None:
    with Session(engine) as s:
        row = s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=json.dumps(value)))
        else:
            row.value = json.dumps(value)
        s.commit()
