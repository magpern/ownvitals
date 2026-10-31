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
from ownvitals.admin.routes import router as admin_router
from ownvitals.auth.routes import router as auth_router
from ownvitals.config import Settings
from ownvitals.connections.routes import router as connections_router
from ownvitals.crypto.keks import load_keyring
from ownvitals.db import (
    check_ready,
    create_engine,
    create_session_factory,
    verify_database_role,
)
from ownvitals.flags.routes import router as flags_router
from ownvitals.headers import SecurityHeaders
from ownvitals.jobs.routes import router as jobs_router

logger = logging.getLogger(__name__)

READY_TIMEOUT_SECONDS = 5.0


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


class ReadyResponse(BaseModel):
    status: Literal["ready", "unavailable"]


api = APIRouter(prefix="/api")

# The routers of the feature packages. Each is empty until its issue fills it, and is included
# here already so those issues do not edit this module to mount their routes.
FEATURE_ROUTERS = (
    auth_router,
    connections_router,
    flags_router,
    jobs_router,
    admin_router,
)


@api.get("/health")
async def health() -> HealthResponse:
    """Liveness: the process is up. Never touches the database."""
    return HealthResponse(status="ok", version=__version__)


@api.get("/ready", responses={503: {"model": ReadyResponse}})
async def ready(request: Request, response: Response) -> ReadyResponse:
    """Readiness: the database answers ``SELECT 1``. No error detail in the body."""
    try:
        async with asyncio.timeout(READY_TIMEOUT_SECONDS):
            await check_ready(request.app.state.session_factory)
    except Exception as exc:  # any failure means "not ready"
        # The exception text can name hosts and users, so only its type is logged.
        logger.warning("database not ready", extra={"error_type": type(exc).__name__})
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
    is required and must match its check row, and web never holds the webhook seed.
    """
    settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        try:
            await verify_database_role(engine)
            session_factory = create_session_factory(engine)
            app.state.keyring = await load_keyring(session_factory, settings, role="web")
            app.state.engine = engine
            app.state.session_factory = session_factory
            yield
        finally:
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
    for router in FEATURE_ROUTERS:
        app.include_router(router)

    if settings.static_dir is not None:
        if settings.static_dir.is_dir():
            _add_single_page_app(app, settings.static_dir)
        else:
            logger.warning("static_dir does not exist; not serving static files")
    # Added last, so it is the outermost middleware: the CSP and Referrer-Policy go on every
    # response, the answers of the feature routers' middleware included.
    app.add_middleware(SecurityHeaders)
    return app
