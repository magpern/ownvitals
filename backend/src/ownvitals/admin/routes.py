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
   in the last 5 minutes (ADR 0003 section 4, admin writes). ``stepped_up`` is the one place
   that decides it: once #11's ``require_admin_stepup`` is on ``main``, switching to it is a
   one-line change there. CSRF and Origin are checked for those methods by the auth middleware
   before any route runs.
3. 503 ``admin_unavailable`` when web has no maint credentials (``AdminDb`` is ``None``).
4. 60 requests per admin and minute (``RequestLimiter``, per process): 429 ``rate_limited``.

An admin refused at 1 (``mfa_required``), 2 or 4 gets an ``admin.request`` audit row with
``outcome = 'refused'``, within the refusal limiter's 20 per admin and 100 per minute. So does an
admin past the gate whose request is then refused for its input: FastAPI's own 422 (reason
``invalid_input``) or an ``AdminRefused`` raised before any ``admin_session``. A body FastAPI
cannot read (400 ``body_unreadable``) or over ``MAX_BODY_BYTES`` (413 ``body_too_large``) is
refused before any dependency runs; the gate then runs in the handler first, so a non-admin
still gets the 403 and an admin's refusal still has its row. Every response carries
``Cache-Control: no-store``.
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
from fastapi.routing import APIRoute, iter_route_contexts
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
    """Whether the session's last step-up is within ``ADMIN_STEPUP_WINDOW`` (the one place that
    decides it; see the module docstring). ``info`` is the auth package's ``SessionInfo``; it is
    typed ``Any`` because guard 8 keeps that class inside ``ownvitals.auth``."""
    return info.stepup_at is not None and info.now - info.stepup_at <= ADMIN_STEPUP_WINDOW


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


# The largest admin request body. The largest valid one is a banner (280 characters, at most 12
# bytes each once JSON-escaped); 16 KiB leaves room without letting a body matter.
MAX_BODY_BYTES: Final = 16 * 1024
BODY_TOO_LARGE: Final = "body_too_large"
BODY_UNREADABLE: Final = "body_unreadable"
# The two body refusals by exactly what raises them, so a 400 or 413 a route raises itself is
# never taken for one: FastAPI's own body-parse failure and ``AdminBodyLimit``'s.
_FASTAPI_BODY_ERROR = "There was an error parsing the body"
_BODY_REFUSALS: dict[tuple[int, object], str] = {
    (400, _FASTAPI_BODY_ERROR): BODY_UNREADABLE,
    (413, repr({"reason": BODY_TOO_LARGE})): BODY_TOO_LARGE,
}


def _body_refusal(exc: object) -> str | None:
    status, detail = getattr(exc, "status_code", None), getattr(exc, "detail", None)
    key = detail if isinstance(detail, str) else repr(detail)
    return _BODY_REFUSALS.get((status, key)) if isinstance(status, int) else None


class AdminBodyLimit:
    """ASGI middleware: an ``/api/admin`` request body over ``MAX_BODY_BYTES``, by its
    ``Content-Length`` or as it streams in, is refused 413 when the route reads it. The refusal
    is raised from ``receive``, so FastAPI's body parsing re-raises it before any dependency,
    and the admin area's handler runs the gate first."""

    def __init__(self, app: Any, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith(ADMIN_PREFIX):
            await self.app(scope, receive, send)
            return
        declared = next(
            (v for k, v in scope.get("headers", []) if k.lower() == b"content-length"), None
        )
        too_large = declared is not None and (
            not declared.isdigit() or int(declared) > self.max_bytes
        )
        seen = 0

        async def limited() -> Any:
            nonlocal seen
            if too_large:
                raise HTTPException(status_code=413, detail={"reason": BODY_TOO_LARGE})
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise HTTPException(status_code=413, detail={"reason": BODY_TOO_LARGE})
            return message

        await self.app(scope, limited, send)


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
    from ownvitals.admin import audit_routes, jobs, settings  # they import this module

    for feature in (audit_routes.router, jobs.router, settings.router):
        app.include_router(feature)

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


def admin_area_routes(routes: list[Any]) -> list[tuple[str, Any]]:
    """Every route of ``routes`` under ``/api/admin`` with its full path, through included
    routers (FastAPI keeps them as nested entries of ``app.routes``)."""
    found: list[tuple[str, Any]] = []
    for context in iter_route_contexts(routes):
        path = context.path or getattr(context.original_route, "path", "") or ""
        if path.startswith(ADMIN_PREFIX):
            found.append((path, context.original_route))
    return found


def gate_problems(routes: list[Any], whoami: Any) -> list[str]:
    """For the router guard: routes under ``/api/admin`` without ``admin_gate`` (or, for the
    auth package's ``whoami``, without ``whoami``'s dependency, passed in as ``whoami``), or that
    are not plain API routes (a mount or a websocket would bypass the check)."""
    problems: list[str] = []
    for path, route in admin_area_routes(routes):
        if not isinstance(route, APIRoute):
            problems.append(f"{path}: not an API route ({type(route).__name__})")
            continue
        is_whoami = path == f"{ADMIN_PREFIX}/whoami"
        needed = whoami if is_whoami else admin_gate
        if needed not in calls(route.dependant):
            name = "require_admin" if is_whoami else "admin_gate"
            problems.append(f"{sorted(route.methods or set())} {path}: no {name}")
    return problems


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
