"""``/api/auth``: the session endpoint and session management (ADR 0003 sections 3 and 4).

Every query here filters on the ``user_id`` of the
request's own session, and another user's session id answers 404, the same as an unknown one.
"""

import secrets
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.cookies import CSRF_COOKIE, clear_session_cookie, set_csrf_cookie
from ownvitals.auth.deps import current_session, request_ip, require_stepup, require_user
from ownvitals.auth.events import record_event
from ownvitals.auth.middleware import csrf_token_for_session
from ownvitals.auth.sessions import (
    SessionInfo,
    list_sessions,
    revoke_session,
    revoke_user_sessions,
)
from ownvitals.context import UserContext
from ownvitals.db import system_session

router = APIRouter(prefix="/api/auth", tags=["auth"])

CSRF_VALUE_MAX = 128


class UserOut(BaseModel):
    id: UUID
    email: str
    role: str


class SessionOut(BaseModel):
    id: UUID
    expires_at: datetime
    recent_auth: bool
    # The session passed its second factor (it is full, ownvitals ADR 0003 section 2).
    mfa: bool
    stepup: bool
    # The account has a confirmed TOTP, so the SPA sends a session that is not full to the code
    # prompt rather than to enrolment.
    totp_enabled: bool
    # What a session that is not full still owes: "enrol" or "verify"; null once it is full.
    second_factor: Literal["enrol", "verify"] | None
    # The signed-in account is an admin whose TOTP check is missing or lapsed, so the page shows
    # the code prompt (ADR 0011 section 6). Always false for any other account; it reports only
    # the caller's own account, and the admin routes decide on their own.
    admin_pending_mfa: bool


class SessionState(BaseModel):
    authenticated: bool
    csrf_token: str
    user: UserOut | None = None
    session: SessionOut | None = None


class SessionItem(BaseModel):
    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    user_agent: str | None
    ip: str | None
    current: bool


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


@router.get("/session")
async def get_session(
    request: Request,
    response: Response,
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> SessionState:
    """Who is signed in, and the CSRF token that applies to the next unsafe request.

    With a valid session the token is ``HMAC(csrf_key, session.id)``. Without one it is the
    value of the anonymous ``__Host-ownvitals_csrf`` cookie, which is set here if missing.
    """
    if info is not None:
        return SessionState(
            authenticated=True,
            csrf_token=csrf_token_for_session(request.app.state.keys, info.id),
            user=UserOut(id=info.user_id, email=info.email, role=info.role),
            session=SessionOut(
                id=info.id,
                expires_at=info.expires_at,
                recent_auth=info.recent_auth,
                mfa=info.full,
                stepup=info.full and info.recent_stepup,
                totp_enabled=info.totp_enabled,
                second_factor=info.second_factor,
                admin_pending_mfa=info.admin_pending_mfa,
            ),
        )
    value = request.cookies.get(CSRF_COOKIE, "")
    if not value or len(value) > CSRF_VALUE_MAX:
        value = secrets.token_urlsafe(32)
        set_csrf_cookie(response, value)
    return SessionState(authenticated=False, csrf_token=value)


@router.get("/sessions")
async def list_own_sessions(
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> list[SessionItem]:
    async with system_session(_factory(request)) as db:
        rows = await list_sessions(db, request.app.state.settings, ctx.user_id)
    return [
        SessionItem(
            id=row.id,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
            user_agent=row.user_agent,
            ip=row.ip,
            current=info is not None and row.id == info.id,
        )
        for row in rows
    ]


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    request: Request,
    response: Response,
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> None:
    """Sign out one of the user's own sessions. 404 for an unknown id and for another user's."""
    try:
        target = UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=404) from None
    async with system_session(_factory(request)) as db:
        deleted = await revoke_session(db, ctx.user_id, target)
    if not deleted:
        raise HTTPException(status_code=404)
    if info is not None and target == info.id:
        clear_session_cookie(response)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> None:
    """Delete this session and clear the cookie. Anonymous: nothing to do."""
    if info is None:
        return
    async with system_session(_factory(request)) as db:
        await revoke_session(db, info.user_id, info.id)
    clear_session_cookie(response)


@router.post("/logout-all", status_code=204)
async def logout_all(
    request: Request,
    response: Response,
    ctx: Annotated[UserContext, Depends(require_stepup)],
) -> None:
    """Sign out everywhere, this browser included (step-up first, ownvitals ADR 0003 section 3)."""
    async with system_session(_factory(request)) as db:
        count = await revoke_user_sessions(db, ctx.user_id)
        await record_event(
            db,
            "logout_everywhere",
            user_id=ctx.user_id,
            ip=request_ip(request),
            detail={"sessions": count},
        )
    clear_session_cookie(response)
