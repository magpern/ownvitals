"""The web app: JSON API under ``/api`` and, optionally, the built single-page app."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ownvitals import __version__
from ownvitals.admin.db import AdminDb
from ownvitals.admin.routes import install as install_admin
from ownvitals.admin.routes import router as admin_router
from ownvitals.auth.setup import attach_state, detach_state
from ownvitals.auth.setup import install as install_auth
from ownvitals.config import Settings
from ownvitals.connections.routes import router as connections_router
from ownvitals.connections.withings_routes import router as withings_connect_router
from ownvitals.crypto.keks import load_keyring
from ownvitals.db import (
    check_ready,
    create_engine,
    create_session_factory,
    verify_database_role,
)
from ownvitals.db.queue_check import install_queue_check, verify_queue
from ownvitals.flags.routes import router as flags_router
from ownvitals.headers import SecurityHeaders
from ownvitals.jobs.routes import router as jobs_router
from ownvitals.notify import sender as outbox_sender
from ownvitals.views.days_routes import router as days_router
from ownvitals.views.findings_routes import router as findings_router
from ownvitals.views.metrics_routes import router as metrics_router
from ownvitals.views.nerve_routes import router as nerve_router
from ownvitals.views.profile_routes import router as profile_router
from ownvitals.views.scores_routes import router as scores_router
from ownvitals.views.status_routes import router as status_router
from ownvitals.withings.webhooks import WebhookMiddleware, load_cache

logger = logging.getLogger(__name__)

READY_TIMEOUT_SECONDS = 5.0


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


class ReadyResponse(BaseModel):
    status: Literal["ready", "unavailable"]


api = APIRouter(prefix="/api")

# The routers of the feature packages. Each is empty until its issue fills it, and is included
# here already so those issues do not edit this module to mount their routes. Accounts and
# sign-in (#10) are wired by ``ownvitals.auth.setup.install``: their routers, the Origin/CSRF
# middleware and the error handlers.
FEATURE_ROUTERS = (
    connections_router,
    withings_connect_router,
    flags_router,
    jobs_router,
    admin_router,
    findings_router,
    status_router,
    metrics_router,
    days_router,
    nerve_router,
    profile_router,
    scores_router,
)


@api.get("/health")
async def health() -> HealthResponse:
    """Liveness: the process is up. Never touches the database."""
    return HealthResponse(status="ok", version=__version__)


@api.get("/ready", responses={503: {"model": ReadyResponse}})
async def ready(request: Request, response: Response) -> ReadyResponse:
    """Readiness: the database answers ``SELECT 1`` and the key ring is verified (ADR 0004
    section 3; a ring whose check was deferred at startup is checked here). No error detail in
    the body."""
    try:
        async with asyncio.timeout(READY_TIMEOUT_SECONDS):
            await check_ready(request.app.state.session_factory)
    except Exception as exc:  # any failure means "not ready"
        # The exception text can name hosts and users, so only its type is logged.
        logger.warning("database not ready", extra={"error_type": type(exc).__name__})
        response.status_code = 503
        return ReadyResponse(status="unavailable")
    try:
        async with asyncio.timeout(READY_TIMEOUT_SECONDS):
            await request.app.state.keyring.verify()
    except Exception as exc:  # an unverified ring: not ready, every use refuses
        logger.warning("key ring not verified", extra={"error_type": type(exc).__name__})
        response.status_code = 503
        return ReadyResponse(status="unavailable")
    return ReadyResponse(status="ready")


def _add_single_page_app(app: FastAPI, static_dir: Path) -> None:
    """Serve ``static_dir``; any other GET outside ``/api`` gets ``index.html``."""
    root = static_dir.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def single_page_app(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)
        try:
            candidate = (root / path).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                return FileResponse(candidate)
        except (OSError, ValueError):  # for example an embedded NUL in the path
            pass
        index = root / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(index)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. The database engine is created in the lifespan, not here.

    The lifespan refuses to start when the database role could bypass row-level security
    (``DatabaseRoleError``, ADR 0002 section 5); the engine checks every later connection too.
    It then loads web's key ring (``KeyRingError``, ADR 0004 sections 3 and 4): the current KEK
    is required and must match its check row, and web never holds the webhook seed. The queue
    check (``QueueDriftError``, ADR 0005 section 3) refuses a missing or altered queue function,
    role or digest, on this startup and on every later connection. Then the
    accounts state (``ownvitals.auth.setup.attach_state``): it refuses to start without the
    ``app_secret_key`` secret file, from which the auth keys are derived (ADR 0003 section 1).
    Then the outbox sender (``ownvitals.notify.sender``, ADR 0007), when an SMTP host is set.
    Last the admin database (#12): without the maint URL or its password file there is none and
    the admin routes answer 503; with them, a role other than exactly ``ownvitals_maint``
    refuses the start (``MaintRoleError``).
    """
    settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        install_queue_check(engine)  # every new connection, before the first one is opened
        admin_db: AdminDb | None = None
        outbox_task = None
        try:
            await verify_database_role(engine)
            session_factory = create_session_factory(engine)
            app.state.keyring = await load_keyring(session_factory, settings, role="web")
            await verify_queue(engine)
            app.state.engine = engine
            app.state.session_factory = session_factory
            attach_state(app, settings, session_factory)
            # The outbox sender (ADR 0007 section 2): web sends what the worker queued.
            outbox_task = outbox_sender.start(
                session_factory,
                app.state.mailer,
                settings.public_base_url,
                settings.enabled_locales,  # intersected with the manifest when used
            )
            admin_db = AdminDb.from_settings(settings)
            if admin_db is not None:
                await admin_db.verify()
            app.state.admin_db = admin_db
            # A failed read leaves the cache empty: POST answers 503 until one succeeds.
            try:
                await load_cache(app)
            except Exception as exc:
                logger.warning("webhook cache not loaded", extra={"error_type": type(exc).__name__})
            yield
        finally:
            await outbox_sender.stop(outbox_task)
            app.state.admin_db = None
            if admin_db is not None:
                await admin_db.dispose()
            detach_state(app)
            await engine.dispose()

    docs_enabled = settings.environment != "prod"
    app = FastAPI(
        title="ownvitals",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if docs_enabled else None,
    )
    app.state.settings = settings
    app.include_router(api)
    # Before the SPA catch-all: Google's callback (``/oauth/google/callback``) is not under /api.
    install_auth(app)
    for router in FEATURE_ROUTERS:
        app.include_router(router)
    install_admin(app)  # the admin area's feature routers and handlers, after install_auth

    if settings.static_dir is not None:
        if settings.static_dir.is_dir():
            _add_single_page_app(app, settings.static_dir)
        else:
            logger.warning("static_dir does not exist; not serving static files")
    # Outside the auth middleware (added earlier, so inner): Withings sends no Origin or CSRF
    # token. The secret in the path is the check. SecurityHeaders stays the outermost layer.
    app.add_middleware(WebhookMiddleware)
    # Added last, so it is the outermost middleware: the CSP and Referrer-Policy go on every
    # response, the answers of the feature routers' middleware included.
    app.add_middleware(SecurityHeaders)
    return app
