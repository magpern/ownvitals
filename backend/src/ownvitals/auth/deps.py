"""From request to ``UserContext`` (ADR 0003 section 2).

``current_context`` is the only way a web request gets a ``UserContext``, and this module is
the only web issuer. A missing cookie and a token that matches no row are both anonymous, and
neither clears the cookie (the token may have just been rotated). A known session that is
expired, idle or belongs to a disabled user is deleted and the cookie is cleared.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, NoReturn
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.clientip import client_ip
from ownvitals.auth.cookies import DEVICE_COOKIE, SESSION_COOKIE
from ownvitals.auth.sessions import Resolution, SessionInfo, resolve_session
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
    """The request's ``UserContext``, or ``None`` when anonymous.

    ``role`` is ``admin`` only for an admin whose session passed TOTP within 12 hours.
    """
    if info is None:
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


async def require_user(
    ctx: Annotated[UserContext | None, Depends(current_context)],
) -> UserContext:
    if ctx is None:
        _refuse(401, "not_authenticated")
    return ctx


async def require_recent_auth(
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> UserContext:
    """Re-authenticated within 10 minutes and, once the user's TOTP is confirmed, a step-up
    within 5 minutes as well (ADR 0003 section 6)."""
    if info is None or not info.recent_auth:
        _refuse(403, "reauth_required")
    if info.totp_enabled and not info.recent_stepup:
        _refuse(403, "stepup_required")
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


async def require_stepup(
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> UserContext:
    """A TOTP or recovery code within 5 minutes."""
    if info is None or not info.recent_stepup:
        _refuse(403, "stepup_required")
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
    cookie the request came with still resolves to a valid session of that user. A revoked,
    expired or rotated-away session, or one of another user, makes it ``False``."""
    token = request.cookies.get(SESSION_COOKIE)
    factory = request.app.state.session_factory
    settings = request.app.state.settings

    def probe_for(user_id: UUID) -> Probe:
        async def still_signed_in() -> bool:
            if not token:
                return False
            resolution = await resolve_session(factory, settings, token)
            return resolution.info is not None and resolution.info.user_id == user_id

        return still_signed_in

    return probe_for
