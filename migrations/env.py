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
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
