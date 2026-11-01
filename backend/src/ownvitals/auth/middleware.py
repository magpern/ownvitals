"""Origin and CSRF checks, ``no-store``, cookie clearing and our own access log.

ADR 0003 section 4: every ``POST``, ``PUT``, ``PATCH`` and ``DELETE`` passes two checks, and
because this is a middleware no route can forget them.

- ``Origin`` must equal the public origin. Without ``Origin``, ``Sec-Fetch-Site`` must be
  ``same-origin``; with neither, the request is refused.
- ``X-CSRF-Token`` must match ``HMAC(csrf_key, session.id)`` when the request carries a valid
  session, else the value of the ``__Host-ownvitals_csrf`` cookie (double submit).

Section 10: the access log replaces uvicorn's. It records method, path, status and time, never
the query string, headers, cookies or body.
"""

import hmac
import logging
import time

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from ownvitals.auth.cookies import CSRF_COOKIE, SESSION_COOKIE, clear_session_cookie
from ownvitals.auth.deps import resolve_request
from ownvitals.auth.keys import Keys, hmac_digest
from ownvitals.config import Settings

logger = logging.getLogger("ownvitals.access")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def csrf_token_for_session(keys: Keys, session_id: object) -> str:
    return hmac_digest(keys.csrf, str(session_id)).hex()


def check_origin(request: Request, settings: Settings) -> str | None:
    """The refusal reason, or ``None`` when the request comes from our own origin."""
    origin = request.headers.get("origin")
    if origin is not None:
        return None if origin == settings.public_base_url else "origin_mismatch"
    site = request.headers.get("sec-fetch-site")
    if site is None:
        return "origin_missing"
    return None if site == "same-origin" else "origin_mismatch"


async def check_csrf(request: Request, settings: Settings, keys: Keys) -> str | None:
    """The refusal reason, or ``None`` when the request may proceed."""
    if (reason := check_origin(request, settings)) is not None:
        return reason
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        return "csrf_token_missing"
    resolution = await resolve_request(request)
    if resolution.info is not None:
        expected = csrf_token_for_session(keys, resolution.info.id)
    else:
        expected = request.cookies.get(CSRF_COOKIE, "")
        if not expected:
            return "csrf_token_invalid"
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        return "csrf_token_invalid"
    return None


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        response: Response | None = None
        if request.method not in SAFE_METHODS:
            reason = await check_csrf(request, request.app.state.settings, request.app.state.keys)
            if reason is not None:
                response = JSONResponse({"detail": {"reason": reason}}, status_code=403)
        if response is None:
            response = await call_next(request)
        if getattr(request.state, "clear_session_cookie", False) and not any(
            SESSION_COOKIE in value for value in response.headers.getlist("set-cookie")
        ):
            clear_session_cookie(response)
        if request.url.path.startswith("/api/") or request.url.path == "/api":
            response.headers["Cache-Control"] = "no-store"
        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return response
