"""Password sign-in and re-authentication (ADR 0003 sections 3, 5, 6 and 9).

Every failure gets the same answer. An unknown email, a user without a password and a disabled
user all cost one argon2 verification, so the timing matches. Failures (and only failures) count
against the limits: the attempt reserves its slots first and releases them on success.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from ownvitals.auth.accounts import grant_admin_if_listed
from ownvitals.auth.cookies import clear_session_cookie, set_device_cookie, set_session_cookie
from ownvitals.auth.deps import (
    current_session,
    request_device_token,
    request_ip,
    request_session_token,
    require_user,
)
from ownvitals.auth.devices import is_known_device, remember_device
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import factory, reserve, user_agent
from ownvitals.auth.passwords import PasswordHasher, PasswordRuleError, normalize_password
from ownvitals.auth.ratelimit import password_buckets
from ownvitals.auth.sessions import (
    SessionGone,
    SessionInfo,
    rotate_session,
    start_session,
)
from ownvitals.auth.tables import users
from ownvitals.context import UserContext
from ownvitals.db import system_session
from ownvitals.emailnorm import normalize_email

router = APIRouter(prefix="/api/auth", tags=["auth"])

FIELD_MAX = 1024
INVALID = {"reason": "invalid_credentials"}


class LoginIn(BaseModel):
    email: str = Field(max_length=FIELD_MAX)
    password: str = Field(max_length=FIELD_MAX)


class ReauthIn(BaseModel):
    password: str = Field(max_length=FIELD_MAX)


class SignedIn(BaseModel):
    id: UUID
    email: str
    role: str


class Done(BaseModel):
    status: str = "ok"


async def _check_password(
    hasher: PasswordHasher, stored: str | None, typed_raw: str
) -> tuple[bool, str | None]:
    """``(matches, normalized password or None)``; one verification whatever the case."""
    try:
        typed = normalize_password(typed_raw)
    except PasswordRuleError:
        typed = None
    if stored is not None and typed is not None:
        return await hasher.verify(stored, typed), typed
    await hasher.verify_dummy(typed or "x")
    return False, typed


@router.post("/login")
async def sign_in(body: LoginIn, request: Request, response: Response) -> SignedIn:
    settings, keys = request.app.state.settings, request.app.state.keys
    hasher: PasswordHasher = request.app.state.password_hasher
    ip, normalized = request_ip(request), normalize_email(body.email)
    device_token = request_device_token(request)
    async with system_session(factory(request)) as db:
        user = (
            await db.execute(
                select(
                    users.c.id,
                    users.c.email,
                    users.c.role,
                    users.c.password_hash,
                    users.c.disabled_at,
                ).where(users.c.email_normalized == normalized)
            )
        ).first()
        known = user is not None and await is_known_device(db, keys, user.id, device_token)
    attempt = await reserve(request, password_buckets(normalized, ip, known_device=known))
    matches, typed = await _check_password(
        hasher, user.password_hash if user else None, body.password
    )
    if user is None or not matches or typed is None or user.disabled_at is not None:
        # A disabled user gets the same answer as a wrong password; refused before any session.
        async with system_session(factory(request)) as db:
            await record_event(
                db, "sign_in_failed", user_id=user.id if user else None, ip=ip, detail=None
            )
        raise HTTPException(status_code=401, detail=INVALID)
    await attempt.succeed()
    rehash = (
        await hasher.hash(typed)
        if user.password_hash and hasher.needs_rehash(user.password_hash)
        else None
    )
    async with system_session(factory(request)) as db:
        await grant_admin_if_listed(db, settings, user.id)  # may revoke every session
        issued = await start_session(
            db,
            settings,
            user.id,
            user_agent=user_agent(request),
            ip=ip,
            replacing_token=request_session_token(request),
        )
        device = await remember_device(db, keys, user.id, device_token)
        if rehash is not None:
            await db.execute(
                update(users).where(users.c.id == user.id).values(password_hash=rehash)
            )
        await record_event(db, "sign_in", user_id=user.id, ip=ip)
        role = (await db.execute(select(users.c.role).where(users.c.id == user.id))).scalar_one()
    set_session_cookie(response, issued)
    set_device_cookie(response, device)
    return SignedIn(id=user.id, email=user.email, role=role)


@router.post("/reauth")
async def reauthenticate(
    body: ReauthIn,
    request: Request,
    response: Response,
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> Done:
    """Re-authentication with the password (section 6): sets ``reauth_at`` and rotates the
    session. Failures count like sign-in failures."""
    if info is None:
        raise HTTPException(status_code=401, detail={"reason": "not_authenticated"})
    keys = request.app.state.keys
    ip, normalized = request_ip(request), normalize_email(info.email)
    async with system_session(factory(request)) as db:
        stored = (
            await db.execute(select(users.c.password_hash).where(users.c.id == ctx.user_id))
        ).scalar_one_or_none()
        known = await is_known_device(db, keys, ctx.user_id, request_device_token(request))
    attempt = await reserve(request, password_buckets(normalized, ip, known_device=known))
    matches, _ = await _check_password(request.app.state.password_hasher, stored, body.password)
    if not matches:
        async with system_session(factory(request)) as db:
            await record_event(db, "reauth_failed", user_id=ctx.user_id, ip=ip)
        raise HTTPException(status_code=401, detail=INVALID)
    await attempt.succeed()
    try:
        async with system_session(factory(request)) as db:
            issued = await rotate_session(
                db, info, reauth=True, user_agent=user_agent(request), ip=ip
            )
            await record_event(db, "reauth", user_id=ctx.user_id, ip=ip)
    except SessionGone:
        clear_session_cookie(response)
        raise HTTPException(status_code=401, detail={"reason": "not_authenticated"}) from None
    set_session_cookie(response, issued)
    return Done()
