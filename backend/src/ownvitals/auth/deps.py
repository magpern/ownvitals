"""From request to ``UserContext`` (ADR 0003 section 2).

``current_context`` is the only way a web request gets a ``UserContext``, and this module is
the only web issuer. A missing cookie and a token that matches no row are both anonymous, and
neither clears the cookie (the token may have just been rotated). A known session that is
expired, idle or belongs to a disabled user is deleted and the cookie is cleared.
"""

from typing import Annotated, Any, Literal, NoReturn
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals import connections
from ownvitals.auth.clientip import client_ip
from ownvitals.auth.cookies import DEVICE_COOKIE, SESSION_COOKIE
from ownvitals.auth.sessions import Resolution, SessionInfo, resolve_session
from ownvitals.auth.tables import users
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
    """Two steps (ADR 0004 section 1): a context without connections, to read the user's
    ``(provider, id)`` pairs in their own session, then the final context with them."""
    bare = UserContext(user_id=user_id, role=role, issuer=issuer_token())
    refs = await connections.connection_refs(factory, bare)
    return UserContext(user_id=user_id, role=role, connections=refs, issuer=issuer_token())


async def operator_contexts(db: AsyncSession) -> list[UserContext]:
    """For the CLI only (``keys rewrap``, ``keys wipe``; ADR 0004 section 1): a context per user
    (role ``user``, no connections), oldest first, for per-owner work with ``app.user_id`` set."""
    ids = (await db.execute(select(users.c.id).order_by(users.c.created_at, users.c.id))).scalars()
    return [UserContext(user_id=user_id, role="user", issuer=issuer_token()) for user_id in ids]


async def purge_contexts(db: AsyncSession) -> list[UserContext]:
    """For the hourly purge only (``ownvitals.auth.purge``; ADR 0009 section 3): a context per user
    (role ``user``, no connections) to delete that user's used or expired Spotify connect flows in
    their own session, since the table is owned and row security is forced. Nothing else may name
    it (``tests/test_user_context_guard.py``)."""
    ids = (await db.execute(select(users.c.id).order_by(users.c.id))).scalars()
    return [UserContext(user_id=user_id, role="user", issuer=issuer_token()) for user_id in ids]


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
    """The client address (ADR 0003 section 9): the peer, or behind a trusted proxy the
    right-most ``X-Forwarded-For`` entry that is not a proxy."""
    settings: Settings = request.app.state.settings
    peer = request.client.host if request.client else None
    return client_ip(
        peer, request.headers.getlist("x-forwarded-for"), settings.trusted_proxy_networks
    )


def request_device_token(request: Request) -> str | None:
    return request.cookies.get(DEVICE_COOKIE)


def request_session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def state(request: Request, name: str) -> Any:
    """``request.app.state.<name>``: ``keys``, ``limiter``, ``password_hasher``, ..."""
    return getattr(request.app.state, name)
