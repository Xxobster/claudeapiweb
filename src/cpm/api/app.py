"""FastAPI application factory with the local security guard (WP2)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from alembic.runtime.migration import MigrationContext
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from cpm import __version__
from cpm.api import (
    budget,
    credentials,
    dashboard,
    lanes,
    locks,
    projects,
    recovery,
    review,
    runs,
    scheduler,
    tasks,
    ui,
    wizard,
)
from cpm.config import Settings, load_settings
from cpm.db.engine import make_engine
from cpm.db.migrate import upgrade
from cpm.lanes.service import LaneService
from cpm.locks.admission import Admission
from cpm.locks.boardpr import JobBoardService
from cpm.locks.external import ExternalWatcher
from cpm.locks.reservations import ReservationService
from cpm.logging_setup import configure_logging
from cpm.providers.keycheck import HttpKeyValidator, KeyValidator, MockKeyValidator
from cpm.recovery.backup import create_backup
from cpm.recovery.reconcile import Recovery
from cpm.review.gate import GateService
from cpm.review.integrate import IntegrationService
from cpm.scheduler.core import Scheduler, run_forever
from cpm.security.childenv import inherited_credential_vars
from cpm.security.middleware import SecurityMiddleware
from cpm.security.secrets_store import KeyringSecretStore, SecretStore
from cpm.security.tickets import TicketStore
from cpm.security.tokens import csrf_for, load_or_create_token
from cpm.workers.supervisor import RunSupervisor

log = logging.getLogger("cpm")


def create_app(
    settings: Settings | None = None,
    secret_store: SecretStore | None = None,
    key_validator: KeyValidator | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    token = load_or_create_token(settings.data_dir)
    engine = make_engine(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_dir)
        if settings.db_path.exists():  # before migrations touch it, and before any recovery
            try:
                create_backup(settings.db_path, settings.data_dir)
            except Exception:  # noqa: BLE001 - a failed backup must not stop CPM from starting
                log.exception("startup backup failed")
        upgrade(settings.db_url)
        app.state.recovery.reconcile()  # before the scheduler: make the records true first
        leaked = inherited_credential_vars()
        if leaked:
            log.warning(
                "credential variables set in the CPM environment (not passed to workers): %s",
                ", ".join(leaked),
            )
        log.info("CPM %s started on %s:%s", __version__, settings.host, settings.port)
        loop_task: asyncio.Task[None] | None = None
        if settings.scheduler_enabled:
            app.state.scheduler.start()
            loop_task = asyncio.create_task(
                run_forever(app.state.scheduler, settings.scheduler_interval_s)
            )
            log.info("scheduler loop started (every %ss)", settings.scheduler_interval_s)
        yield
        if loop_task is not None:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
            # cancelling the await does not stop a tick running in its thread: drain it first
            await asyncio.to_thread(app.state.scheduler.stop)
        app.state.gate.wait_idle(2.0)
        app.state.supervisor.shutdown()
        engine.dispose()

    app = FastAPI(
        title="Claude Project Manager",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.secrets = secret_store or KeyringSecretStore()
    app.state.key_validator = key_validator or MockKeyValidator()
    app.state.live_validator = HttpKeyValidator()
    app.state.lanes = LaneService(engine)
    app.state.supervisor = RunSupervisor(
        engine, settings, app.state.secrets, lanes=app.state.lanes
    )
    app.state.reservations = ReservationService(
        engine, liveness=lambda task_id: app.state.supervisor.live_run_for_task(task_id) is not None
    )
    app.state.recovery = Recovery(
        engine,
        settings,
        is_live=app.state.supervisor.is_live,
        reservations=app.state.reservations,
    )
    app.state.admission = Admission(engine, app.state.reservations)
    app.state.gate = GateService(
        engine, app.state.supervisor, app.state.supervisor.worktrees, lanes=app.state.lanes
    )
    app.state.integration = IntegrationService(
        engine, app.state.supervisor.worktrees, app.state.secrets
    )
    app.state.jobboard = JobBoardService(engine, app.state.supervisor.worktrees, app.state.secrets)
    app.state.scheduler = Scheduler(
        engine, app.state.supervisor, app.state.admission, app.state.reservations,
        ExternalWatcher(engine), gate=app.state.gate,
        resources_for=app.state.lanes.resources, start_args_for=app.state.lanes.start_args,
    )  # fmt: skip
    app.state.tickets = TicketStore()

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc: RequestValidationError) -> JSONResponse:
        # The default handler echoes the submitted input, which would reflect an API key.
        errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    app.include_router(credentials.router)
    app.include_router(wizard.router)
    app.include_router(projects.router)
    app.include_router(tasks.router)
    app.include_router(runs.router)
    app.include_router(review.router)
    app.include_router(lanes.router)
    app.include_router(locks.router)
    app.include_router(scheduler.router)
    app.include_router(recovery.router)
    app.include_router(dashboard.router)
    app.include_router(ui.router)
    ui.mount_static(app)
    app.include_router(budget.router)

    @app.get("/api/session")
    def session() -> dict[str, str]:
        return {"csrf": csrf_for(token)}

    @app.get("/health")
    def health() -> dict[str, object]:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            revision = MigrationContext.configure(conn).get_current_revision()
        return {"status": "ok", "version": __version__, "db_revision": revision}

    app.add_middleware(
        SecurityMiddleware, token=token, port=settings.port, tickets=app.state.tickets
    )
    return app
