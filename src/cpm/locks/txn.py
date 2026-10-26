"""`BEGIN IMMEDIATE` transactions: the write lock is taken up front, so a read-check-write
sequence (is the resource free? then grant it) cannot interleave with another writer."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine


@contextmanager
def immediate(engine: Engine) -> Iterator[Connection]:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.exec_driver_sql("ROLLBACK")
            raise
        conn.exec_driver_sql("COMMIT")
