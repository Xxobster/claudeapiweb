from alembic import context
from sqlalchemy import create_engine, event

from cpm.db.engine import _apply_pragmas
from cpm.db.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    event.listen(engine, "connect", _apply_pragmas)
    with engine.connect() as conn:
        # SQLite rebuilds a table for most ALTERs (batch mode) by dropping the old one. With
        # foreign keys ON that DROP would run the ON DELETE actions and wipe referencing rows
        # (deps, runs, usage...). The documented procedure: FK off for the migration (the pragma
        # is a no-op inside a transaction, so it is set first), check integrity, FK back on.
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        conn.commit()  # the PRAGMA above autobegan the SQLAlchemy transaction
        violations = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign key violations after migration: {violations[:5]}")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


run_migrations_online()
