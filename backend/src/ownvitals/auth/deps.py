"""From request to ``UserContext`` (ADR 0003 section 2).

``current_context`` is the only way a web request gets a ``UserContext``, and this module is
the only web issuer. A missing cookie and a token that matches no row are both anonymous, and
neither clears the cookie (the token may have just been rotated). A known session that is
expired, idle, belongs to a disabled user, or never passed its second factor within ten minutes
is deleted and the cookie is cleared.

TOTP is mandatory (ownvitals ADR 0003 section 2): a session yields a ``UserContext`` only once it
is full. The middleware's gate (``gate.py``) refuses every other route to a session that is not.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, NoReturn
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.clientip import client_ip
from ownvitals.auth.cookies import DEVICE_COOKIE, SESSION_COOKIE
from ownvitals.auth.sessions import (
    ADMIN_STEPUP_WINDOW,
    Resolution,
    SessionInfo,
    resolve_session,
)
from ownvitals.config import Settings
from ownvitals.context import UserContext, issuer_token

_RESOLUTION = "auth_resolution"
_CONTEXT = "auth_context"


async def resolve_request(request: Request) -> Resolution:
    """Resolve the request's session cookie once per request (the CSRF check shares it)."""
    cached: Resolution | None = getattr(request.state, _RESOLUTION, None)
    if cached is not None:
        return cached
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        resolution = Resolution("anonymous")  # no database access without a cookie
    else:
        resolution = await resolve_session(
            request.app.state.session_factory, request.app.state.settings, token
        )
    if resolution.outcome == "refused":
        request.state.clear_session_cookie = True
    setattr(request.state, _RESOLUTION, resolution)
    return resolution


async def current_session(request: Request) -> SessionInfo | None:
    """The valid session of the request, or ``None`` when anonymous."""
    return (await resolve_request(request)).info


async def current_context(
    request: Request, info: Annotated[SessionInfo | None, Depends(current_session)]
) -> UserContext | None:
    """The request's ``UserContext``, or ``None`` when anonymous or when the session has not
    passed its second factor (ownvitals ADR 0003 section 2: only a full session gets one).

    ``role`` is ``admin`` only for an admin whose session passed TOTP within 12 hours.
    """
    if info is None or not info.full:
        return None
    cached: UserContext | None = getattr(request.state, _CONTEXT, None)
    if cached is None:
        cached = await issue_context(request.app.state.session_factory, info.user_id, info.role)
        setattr(request.state, _CONTEXT, cached)
    return cached


async def issue_context(
    factory: async_sessionmaker[AsyncSession], user_id: UUID, role: Literal["user", "admin"]
) -> UserContext:
    """The request's context: the user and the role, no connections yet. ``factory`` is where
    #13 reads the user's ``(provider, id)`` pairs in their own session (ADR 0004 section 1): a
    bare context first, then the final one with them, as classbeat does."""
    del factory  # the connections layer (#13) reads the refs through it
    return UserContext(user_id=user_id, role=role, issuer=issuer_token())


def _refuse(status: int, reason: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"reason": reason})


async def require_session(
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> SessionInfo:
    """Any valid session, full or not: only for the second-factor routes that a session without
    one may reach (``gate.SECOND_FACTOR_ROUTES``). Never yields a ``UserContext``."""
    if info is None:
        _refuse(401, "not_authenticated")
    return info


async def require_user(
    ctx: Annotated[UserContext | None, Depends(current_context)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> UserContext:
    """A full session: 401 ``not_authenticated`` when anonymous, 403 ``two_factor_required``
    for a session that has not passed its second factor."""
    if ctx is None:
        if info is not None:
            _refuse(403, "two_factor_required")
        _refuse(401, "not_authenticated")
    return ctx


async def require_stepup(
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> UserContext:
    """A full session with a TOTP or recovery code within the step-up window
    (``stepup_window_minutes``, 10 minutes; ownvitals ADR 0003 section 4), else 403
    ``step_up_required``. For: replacing, removing or resetting the TOTP, new recovery codes,
    log out everywhere, and (with ``require_sensitive``) every action that also needs recent
    authentication."""
    if info is None or not info.recent_stepup:
        _refuse(403, "step_up_required")
    return ctx


async def require_sensitive(
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> UserContext:
    """Recent authentication (password or Google within 10 minutes, else 403
    ``reauth_required``) and a step-up (else 403 ``step_up_required``): connect, reconnect or
    disconnect a provider, delete the account, change or set the password, change the email,
    link or unlink Google (ownvitals ADR 0003 section 4)."""
    if info is None or not info.recent_auth:
        _refuse(403, "reauth_required")
    if not info.recent_stepup:
        _refuse(403, "step_up_required")
    return ctx


async def require_admin(
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> UserContext:
    """An admin whose session passed TOTP within 12 hours."""
    if info is None or info.stored_role != "admin":
        _refuse(403, "forbidden")
    if ctx.role != "admin":
        _refuse(403, "mfa_required")
    return ctx


async def require_admin_stepup(
    ctx: Annotated[UserContext, Depends(require_admin)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> UserContext:
    """An admin write: ``require_admin`` and a step-up within 5 minutes (ADR 0003 section 4,
    classbeat ADR 0011 section 6), else 403 ``step_up_required``."""
    if info is None or not info.stepup_within(ADMIN_STEPUP_WINDOW):
        _refuse(403, "step_up_required")
    return ctx


def request_ip(request: Request) -> str:
    """The client address (ADR 0003 section 8, ``clientip``): the peer, or behind a trusted
    proxy SWAG's ``CF-Connecting-IP`` or the right-most ``X-Forwarded-For`` entry that is not a
    proxy."""
    settings: Settings = request.app.state.settings
    peer = request.client.host if request.client else None
    return client_ip(
        peer,
        request.headers.getlist("x-forwarded-for"),
        settings.trusted_proxy_networks,
        cf_connecting_ip=request.headers.getlist("cf-connecting-ip"),
        cf_proxies=settings.cf_connecting_ip_networks,
    )


def request_device_token(request: Request) -> str | None:
    return request.cookies.get(DEVICE_COOKIE)


def request_session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def state(request: Request, name: str) -> Any:
    """``request.app.state.<name>``: ``keys``, ``limiter``, ``password_hasher``, ..."""
    return getattr(request.app.state, name)


Probe = Callable[[], Awaitable[bool]]


def session_probe(request: Request) -> Callable[[UUID], Probe]:
    """For a long-lived response of ``user_id`` (the jobs event stream): whether the session
    cookie the request came with still resolves to a valid, full session of that user. A
    revoked, expired or rotated-away session, one that lost its second factor, or one of another
    user, makes it ``False``."""
    token = request.cookies.get(SESSION_COOKIE)
    factory = request.app.state.session_factory
    settings = request.app.state.settings

    def probe_for(user_id: UUID) -> Probe:
        async def still_signed_in() -> bool:
            if not token:
                return False
            resolution = await resolve_session(factory, settings, token)
            info = resolution.info
            return info is not None and info.full and info.user_id == user_id

        return still_signed_in

    return probe_for
