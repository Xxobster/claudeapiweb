"""FastAPI application factory. Security middleware (token, Origin/Host, CSRF) arrives in WP2."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager

from alembic.runtime.migration import MigrationContext
from fastapi import FastAPI
from sqlalchemy import text

from cpm import __version__
from cpm.config import Settings, load_settings
from cpm.db.engine import make_engine
from cpm.db.migrate import upgrade
from cpm.logging_setup import configure_logging

log = logging.getLogger("cpm")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine = make_engine(settings.db_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Iterator[None]:
        configure_logging(settings.log_level, settings.log_dir)
        upgrade(settings.db_url)
        log.info("CPM %s started on %s:%s", __version__, settings.host, settings.port)
        yield
        engine.dispose()

    app = FastAPI(title="Claude Project Manager", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine

    @app.get("/health")
    def health() -> dict[str, object]:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            revision = MigrationContext.configure(conn).get_current_revision()
        return {"status": "ok", "version": __version__, "db_revision": revision}

    return app
