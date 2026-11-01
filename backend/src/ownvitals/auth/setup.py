"""Wiring: what ``create_app`` adds for accounts and sessions (``ownvitals.web``)."""

import logging

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.admin_routes import router as admin_router
from ownvitals.auth.email_routes import router as email_router
from ownvitals.auth.identity_routes import router as identity_router
from ownvitals.auth.keys import Keys
from ownvitals.auth.mfa_routes import router as mfa_router
from ownvitals.auth.middleware import AuthMiddleware
from ownvitals.auth.notices import register_security_notifier
from ownvitals.auth.notifier import MailNotifier
from ownvitals.auth.oauth_client import OAuthRuntime
from ownvitals.auth.oauth_handoff import router as handoff_router
from ownvitals.auth.oauth_providers import enabled_providers
from ownvitals.auth.oauth_routes import router as oauth_router
from ownvitals.auth.passwords import PasswordHashBusy, PasswordHasher
from ownvitals.auth.ratelimit import RateLimited, RateLimiter
from ownvitals.auth.reset_routes import router as reset_router
from ownvitals.auth.routes import router
from ownvitals.auth.signin_routes import router as signin_router
from ownvitals.auth.signup_routes import router as signup_router
from ownvitals.config import Settings
from ownvitals.log import describe_database_error
from ownvitals.mail import Mailer

logger = logging.getLogger(__name__)

# Routes whose request bodies can hold a password, code or token, and the jobs API (its bodies
# are the user's own input; its routes validate them themselves, this is the backstop).
SECRET_INPUT_PATHS = ("/api/auth/", "/api/connections", "/api/jobs")


def install(app: FastAPI) -> None:
    """Routes, the Origin/CSRF middleware and the 429 and 503 handlers."""
    app.include_router(router)
    for flow in (signup_router, signin_router, reset_router, email_router):
        app.include_router(flow)
    app.include_router(mfa_router)
    # OAuth: the callback is not under /api and must precede the SPA catch-all (web.create_app
    # installs auth first). "pending" routes before the "{provider}" ones.
    for oauth in (handoff_router, oauth_router, identity_router):
        app.include_router(oauth)
    app.include_router(admin_router)
    app.add_middleware(AuthMiddleware)

    @app.exception_handler(RateLimited)
    async def rate_limited(_: Request, exc: Exception) -> JSONResponse:
        retry_after = exc.retry_after if isinstance(exc, RateLimited) else 60
        return JSONResponse(
            {"detail": {"reason": "rate_limited"}},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    @app.exception_handler(DBAPIError)
    async def database_error(_: Request, exc: Exception) -> JSONResponse:
        # Class and SQLSTATE only: the message carries row values (emails, hashes).
        text = describe_database_error(exc) if isinstance(exc, DBAPIError) else "database error"
        logger.error("database error: %s", text)
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)

    @app.exception_handler(RequestValidationError)
    async def validation_failed(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, RequestValidationError):
            raise exc
        if not request.url.path.startswith(SECRET_INPUT_PATHS):
            return await request_validation_exception_handler(request, exc)
        # FastAPI's default echoes ``input`` (a password, code or token): drop it on these routes.
        errors = [
            {key: value for key, value in error.items() if key not in ("input", "ctx", "url")}
            for error in exc.errors()
        ]
        return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)

    @app.exception_handler(PasswordHashBusy)
    async def hashing_busy(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            {"detail": {"reason": "busy"}}, status_code=503, headers={"Retry-After": "5"}
        )


def attach_state(
    app: FastAPI, settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Derive the keys and build the limiter and the hashing pool. Refuses to start without
    the ``app_secret_key`` secret file."""
    keys = Keys.from_settings(settings)
    app.state.keys = keys
    app.state.limiter = RateLimiter(factory, keys)
    app.state.password_hasher = PasswordHasher(settings.password_hash_workers)
    app.state.mailer = Mailer(settings)
    app.state.oauth = OAuthRuntime(enabled_providers(settings))
    # httpx logs each request URL at INFO; the OAuth ones are not worth a log line.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # The security notices (TOTP enrolled, repeated MFA failures) go out as mail. The notifier
    # is process-global (``auth.notices``): the last app created in a process wins, and
    # ``detach_state`` clears it. Fine for one app per process; tests that build several apps
    # must not rely on which mailer a notice goes through.
    register_security_notifier(MailNotifier(app.state.mailer))


def detach_state(app: FastAPI) -> None:
    register_security_notifier(None)
    hasher: PasswordHasher | None = getattr(app.state, "password_hasher", None)
    if hasher is not None:
        hasher.close()
