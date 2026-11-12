"""The admin router shell (classbeat ADR 0011 section 6; ownvitals #12): every ``/api/admin``
route of the admin area goes through ``admin_gate``; the feature modules (``audit_routes``,
``settings``, ``jobs``) only add routes to a router ``admin_router()`` returns.
``tests/test_admin_routes_guard.py`` enumerates the app's routes and fails for one under
``/api/admin`` without the gate (``whoami`` of ``ownvitals.auth`` needs ``require_admin``).

The gate, in this order:

1. 401 without a session; 403 ``forbidden`` for a caller whose stored role is not ``admin`` (no
   audit row, no maint connection); 403 ``mfa_required`` for an admin without TOTP in the last
   12 hours (the admin window, ADR 0003 section 7).
2. For every method but GET and HEAD: 403 ``step_up_required`` without a TOTP or recovery code
   within 5 minutes (ADR 0003 section 4, admin writes). ``stepped_up`` calls
   ``info.stepup_within(ADMIN_STEPUP_WINDOW)``, the same rule as ``require_admin_stepup``: five
   minutes, and never wider than the configured step-up window. That dependency is not mounted
   on the route; it would answer 403 before ``refused`` writes the audit row. CSRF and Origin
   are checked for those methods by the auth middleware before any route runs.
3. 503 ``admin_unavailable`` when web has no maint credentials (``AdminDb`` is ``None``).
4. 60 requests per admin and minute (``RequestLimiter``, per process): 429 ``rate_limited``.

An admin refused at 1 (``mfa_required``), 2 or 4 gets an ``admin.request`` audit row with
``outcome = 'refused'``, within the refusal limiter's 20 per admin and 100 per minute. So does an
admin past the gate whose request is then refused for its input: FastAPI's own 422 (reason
``invalid_input``) or an ``AdminRefused`` raised before any ``admin_session``. A body FastAPI
cannot read (400 ``body_unreadable``) or over ``MAX_BODY_BYTES`` (413 ``body_too_large``,
``ownvitals.admin.body_limit``) is refused before any dependency runs; the gate then runs in
the handler first, so a non-admin still gets the 403 and an admin's refusal still has its row.
Every response carries ``Cache-Control: no-store``.
"""

import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware

from ownvitals.admin.audit import METHOD_PATTERN, ROUTE_PATTERN
from ownvitals.admin.db import Actor, AdminDb, AdminRefused, audit_refusal
from ownvitals.auth.deps import current_context, current_session, request_ip
from ownvitals.context import UserContext

GATE_ACTION: Final = "admin.request"
SAFE_METHODS: Final = frozenset({"GET", "HEAD"})
ADMIN_PREFIX: Final = "/api/admin"
# ADR 0003 section 4: admin writes need a TOTP or recovery code within 5 minutes.
ADMIN_STEPUP_WINDOW: Final = timedelta(minutes=5)
STEPUP_REASON: Final = "step_up_required"


def stepped_up(info: Any) -> bool:
    """Whether the session's last step-up is inside the admin write window.

    ``info.stepup_within(ADMIN_STEPUP_WINDOW)`` is the same rule as ``require_admin_stepup``
    (see the module docstring). ``info`` is the auth package's ``SessionInfo``; it is typed
    ``Any`` because guard 8 keeps that class inside ``ownvitals.auth``.
    """
    return bool(info.stepup_within(ADMIN_STEPUP_WINDOW))


def _refuse(status: int, reason: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"reason": reason})


def _ip_hash(request: Request) -> bytes:
    """HMAC-SHA256 of the client address under the rate-limit key, never the address itself."""
    key: bytes = request.app.state.keys.rate_limit
    return hmac.new(
        key, b"admin-audit-ip\x00" + request_ip(request).encode(), hashlib.sha256
    ).digest()


def _gate_params(request: Request) -> dict[str, str]:
    """The ``admin.request`` params: the route template and method, or ``/api/admin`` and
    ``OTHER`` when one would not pass ``RequestParams``, so a refusal keeps its status and its
    row instead of turning into a 500."""
    route: Any = request.scope.get("route")
    path = getattr(route, "path", None)
    method = request.method
    return {
        "method": method if re.fullmatch(METHOD_PATTERN, method) else "OTHER",
        "route": path
        if isinstance(path, str) and re.fullmatch(ROUTE_PATTERN, path)
        else ADMIN_PREFIX,
    }


def admin_db(request: Request) -> AdminDb | None:
    return getattr(request.app.state, "admin_db", None)


def require_db(request: Request) -> AdminDb:
    """The admin database past the gate (which answered 503 without it)."""
    db = admin_db(request)
    if db is None:
        raise AdminRefused("admin_unavailable", 503)
    return db


async def admin_gate(
    request: Request,
    response: Response,
    info: Annotated[Any, Depends(current_session)],
    ctx: Annotated[UserContext | None, Depends(current_context)],
) -> Actor:
    """The admin area's one dependency (module docstring); returns the audited ``Actor``."""
    response.headers["Cache-Control"] = "no-store"
    if ctx is None or info is None:
        _refuse(401, "not_authenticated")
    if info.stored_role != "admin":
        _refuse(403, "forbidden")  # not an admin: no audit row and no maint connection
    db = admin_db(request)
    actor = Actor(
        user_id=info.user_id,
        email=info.email,
        request_id=secrets.token_hex(16),
        ip_hash=_ip_hash(request),
    )

    async def refused(status: int, reason: str, headers: dict[str, str] | None = None) -> NoReturn:
        if db is not None:
            await audit_refusal(db, actor, GATE_ACTION, reason, params=_gate_params(request))
        raise HTTPException(status_code=status, detail={"reason": reason}, headers=headers)

    if ctx.role != "admin":
        await refused(403, "mfa_required")
    if request.method not in SAFE_METHODS and not stepped_up(info):
        await refused(403, STEPUP_REASON)
    if db is None:
        _refuse(503, "admin_unavailable")
    allowed, retry_after = db.requests.check(info.user_id)
    if not allowed:
        await refused(429, "rate_limited", {"Retry-After": str(retry_after)})
    request.state.admin_actor = actor  # past the gate: an input refusal is audited (install)
    return actor


AdminActor = Annotated[Actor, Depends(admin_gate)]


def admin_router() -> APIRouter:
    """A router under ``/api/admin`` whose every route depends on ``admin_gate``."""
    return APIRouter(prefix=ADMIN_PREFIX, tags=["admin"], dependencies=[Depends(admin_gate)])


# The shell's own router; ``ownvitals.web`` includes it, ``install`` the feature routers.
router = admin_router()


async def _audit_input_refusal(request: Request, reason: str) -> None:
    """An ``admin.request`` ``refused`` row for a request that passed the gate and was then
    refused for its input. A request the gate refused has no actor here (the gate wrote its own
    row), and nothing is written without maint."""
    actor = getattr(request.state, "admin_actor", None)
    db = admin_db(request)
    if isinstance(actor, Actor) and db is not None:
        await audit_refusal(db, actor, GATE_ACTION, reason, params=_gate_params(request))


async def _gate_before_validation(request: Request) -> JSONResponse | None:
    """FastAPI refuses a body it cannot read before any dependency runs, so ``admin_gate`` would
    not have run: a non-admin would learn that the route takes a JSON body instead of getting
    the 403, and an admin's refusal would have no row. The gate runs here then, with its own
    refusals and rows; ``None`` once it passed."""
    route: Any = request.scope.get("route")
    if getattr(request.state, "admin_actor", None) is not None or not isinstance(route, APIRoute):
        return None
    if admin_gate not in calls(route.dependant):
        return None  # ``whoami``: not the admin area's gate
    info = await current_session(request)
    ctx = await current_context(request, info)
    try:
        await admin_gate(request, Response(), info, ctx)
    except HTTPException as exc:
        return JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers={**(exc.headers or {}), "Cache-Control": "no-store"},
        )
    return None


async def _http_error(_request: Request, exc: Exception) -> Response:
    """FastAPI's own answer to an ``HTTPException`` (``{"detail": ...}`` with its headers), for
    when no other handler was installed before this one."""
    status = getattr(exc, "status_code", 500)
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        {"detail": getattr(exc, "detail", None)}, status_code=status, headers=headers
    )


def install(app: Any) -> None:
    """The feature routers, the handler that turns an ``AdminRefused`` into its response, the
    audit row of an admin request's 422, 400 or 413, and the body limit. Call it after
    ``ownvitals.auth.setup.install``: it wraps the handlers installed there."""
    from ownvitals.admin import anomaly_routes, audit_routes, jobs, settings, users  # see below
    from ownvitals.admin.body_limit import AdminBodyLimit, _body_refusal  # it imports this one

    for feature in (anomaly_routes, audit_routes, jobs, settings, users):  # import this module
        app.include_router(feature.router)

    previous = app.exception_handlers.get(
        RequestValidationError, request_validation_exception_handler
    )

    @app.exception_handler(RequestValidationError)
    async def validation_failed(request: Request, exc: Exception) -> Response:
        if not request.url.path.startswith(ADMIN_PREFIX):
            return await previous(request, exc)
        refused = await _gate_before_validation(request)
        if refused is not None:
            return refused
        await _audit_input_refusal(request, "invalid_input")
        response = await previous(request, exc)
        response.headers["Cache-Control"] = "no-store"
        return response

    previous_http = app.exception_handlers.get(StarletteHTTPException, _http_error)

    @app.exception_handler(StarletteHTTPException)
    async def body_refused(request: Request, exc: Exception) -> Response:
        """A body FastAPI could not read (400) or ``AdminBodyLimit`` refused (413) on an admin
        route: the gate first, then the refusal with its row. Every other HTTP error goes to the
        previous handler unchanged."""
        reason = _body_refusal(exc)
        if reason is None or not request.url.path.startswith(ADMIN_PREFIX):
            return await previous_http(request, exc)
        refused = await _gate_before_validation(request)
        if refused is not None:
            return refused
        await _audit_input_refusal(request, reason)
        response = await previous_http(request, exc)
        response.headers["Cache-Control"] = "no-store"
        return response

    # Innermost, not ``add_middleware`` (outermost): a ``BaseHTTPMiddleware``'s task group would
    # wrap the 413 raised from ``receive`` in an ``ExceptionGroup``, which FastAPI's body parsing
    # turns into its generic 400. Inside it, the 413 reaches FastAPI as is.
    if app.middleware_stack is not None:  # pragma: no cover
        raise RuntimeError("admin.install must run before the app starts")
    app.user_middleware.append(Middleware(AdminBodyLimit))

    @app.exception_handler(AdminRefused)
    async def admin_refused(request: Request, exc: Exception) -> JSONResponse:
        status, reason = (
            (exc.status, exc.reason) if isinstance(exc, AdminRefused) else (409, "refused")
        )
        if isinstance(exc, AdminRefused) and not exc.audited:
            await _audit_input_refusal(request, reason)
        return JSONResponse(
            {"detail": {"reason": reason}},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )


def calls(dependant: Any) -> set[Any]:
    """Every callable a route's dependency tree calls."""
    found: set[Any] = set()
    todo = [dependant]
    while todo:
        node = todo.pop()
        if node.call is not None:
            found.add(node.call)
        todo.extend(node.dependencies)
    return found
