"""Password reset and password change (ADR 0003 sections 5 and 6).

Reset: the request answers the same for any address and mails a 30-minute link (token in the URL
fragment, posted to the API). Completing it signs the user out everywhere, deletes their known
devices and every other outstanding token, and can remove linked sign-in methods in the same
step: the reset is the proof, so that needs neither recent auth nor step-up. Change: recent
authentication and a step-up, revoking every other session.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth import mails
from ownvitals.auth.accounts import find_user
from ownvitals.auth.clientip import bucket_ip
from ownvitals.auth.deps import current_session, request_ip, require_sensitive
from ownvitals.auth.devices import forget_devices
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import EmailAddress, factory, mailer, reserve
from ownvitals.auth.hooks import ResetLinkSender
from ownvitals.auth.passwords import PasswordRuleError, normalize_password
from ownvitals.auth.ratelimit import (
    MAIL_ACCOUNT,
    MAIL_IP,
    MAIL_PAIR,
    PASSWORD_CHANGE,
    SIGN_IN_IP,
    Bucket,
)
from ownvitals.auth.sessions import SessionInfo, revoke_user_sessions
from ownvitals.auth.signup_routes import Accepted, refuse
from ownvitals.auth.tables import identities, users
from ownvitals.auth.tokens import (
    RESET_LIFETIME,
    consume_token,
    delete_outstanding_tokens,
    issue_token,
    peek_token,
)
from ownvitals.config import Settings
from ownvitals.context import UserContext
from ownvitals.db import system_session
from ownvitals.emailnorm import normalize_email
from ownvitals.mail import Mailer, send_logged

router = APIRouter(prefix="/api/auth", tags=["auth"])

FIELD_MAX = 1024
MAX_REMOVALS = 10
PURPOSE = "reset_password"


class ResetRequestIn(BaseModel):
    email: EmailAddress


class TokenIn(BaseModel):
    token: str = Field(max_length=FIELD_MAX)


class LinkedMethod(BaseModel):
    id: UUID
    provider: str


class ResetInfo(BaseModel):
    methods: list[LinkedMethod]


class ResetCompleteIn(BaseModel):
    token: str = Field(max_length=FIELD_MAX)
    password: str = Field(max_length=FIELD_MAX)
    remove_methods: list[UUID] = Field(
        default_factory=lambda: list[UUID](), max_length=MAX_REMOVALS
    )


class ChangePasswordIn(BaseModel):
    new_password: str = Field(max_length=FIELD_MAX)


class Done(BaseModel):
    status: str = "ok"


async def reset_mail(settings: Settings, db: AsyncSession, user_id: UUID, email: str):
    """Issue a reset token for the user and build the mail."""
    token = await issue_token(db, PURPOSE, user_id, RESET_LIFETIME)
    return mails.password_reset(settings, email, token)


def make_reset_link_sender(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], mail: Mailer
) -> ResetLinkSender:
    """The sender ``auth.register_reset_link_sender`` takes: ``admin mfa-reset`` mails the
    user a reset link (awaited: the CLI has no response to send first)."""

    async def send(user_id: UUID, email: str) -> None:
        async with system_session(session_factory) as db:
            message = await reset_mail(settings, db, user_id, email)
        await mail.send(message)

    return send


@router.post("/password/reset/request", status_code=202)
async def request_reset(
    body: ResetRequestIn, request: Request, background: BackgroundTasks
) -> Accepted:
    normalized, ip = normalize_email(body.email), bucket_ip(request_ip(request))
    await reserve(
        request,
        [
            Bucket(MAIL_PAIR, f"{normalized}|{ip}"),
            Bucket(MAIL_ACCOUNT, normalized),
            Bucket(MAIL_IP, ip),
        ],
    )
    async with system_session(factory(request)) as db:
        user = await find_user(db, body.email)
        message = None
        if user is not None and not user.disabled:
            message = await reset_mail(request.app.state.settings, db, user.id, user.email)
    if message is not None:
        background.add_task(send_logged, mailer(request), message)
    return Accepted()


async def _invalid(request: Request) -> HTTPException:
    await reserve(request, [Bucket(SIGN_IN_IP, bucket_ip(request_ip(request)))])
    return refuse(400, "invalid_token")


@router.post("/password/reset/info")
async def reset_info(body: TokenIn, request: Request) -> ResetInfo:
    """The linked sign-in methods the reset page offers to remove. Does not consume the token."""
    async with system_session(factory(request)) as db:
        token = await peek_token(db, PURPOSE, body.token)
        rows = (
            []
            if token is None
            else (
                await db.execute(
                    select(identities.c.id, identities.c.provider).where(
                        identities.c.user_id == token.user_id
                    )
                )
            ).all()
        )
    if token is None:
        raise await _invalid(request)
    return ResetInfo(methods=[LinkedMethod(id=r.id, provider=r.provider) for r in rows])


@router.post("/password/reset/complete")
async def complete_reset(body: ResetCompleteIn, request: Request) -> Done:
    try:
        password = normalize_password(body.password)
    except PasswordRuleError:
        raise refuse(400, "password_rule") from None
    ip = request_ip(request)
    # ADR 0003 section 9: take the per-IP slot first (released on success), and hash only for
    # a live token, so junk requests never occupy the bounded hashing pool.
    attempt = await reserve(request, [Bucket(SIGN_IN_IP, bucket_ip(ip))])
    wanted = set(body.remove_methods)
    async with system_session(factory(request)) as db:
        live = await peek_token(db, PURPOSE, body.token)
        owned_now = set()
        if live is not None and wanted:
            owned_now = set(
                (
                    await db.execute(
                        select(identities.c.id).where(
                            identities.c.user_id == live.user_id, identities.c.id.in_(wanted)
                        )
                    )
                ).scalars()
            )
    if live is None:
        raise refuse(400, "invalid_token")
    if owned_now != wanted:
        raise HTTPException(status_code=404)  # before hashing; the token stays unused
    password_hash = await request.app.state.password_hasher.hash(password)
    async with system_session(factory(request)) as db:
        token = await consume_token(db, PURPOSE, body.token)
        if token is not None:
            owned = set(
                (
                    await db.execute(
                        select(identities.c.id).where(
                            identities.c.user_id == token.user_id, identities.c.id.in_(wanted)
                        )
                    )
                ).scalars()
            )
            if owned != wanted:
                # 404 for another user's id, as for an unknown one; rolls back, token unused.
                raise HTTPException(status_code=404)
            await db.execute(
                update(users).where(users.c.id == token.user_id).values(password_hash=password_hash)
            )
            await db.execute(
                delete(identities).where(
                    identities.c.user_id == token.user_id, identities.c.id.in_(wanted)
                )
            )
            await revoke_user_sessions(db, token.user_id)
            await forget_devices(db, token.user_id)
            await delete_outstanding_tokens(db, token.user_id)
            await record_event(
                db,
                "password_reset",
                user_id=token.user_id,
                ip=ip,
                detail={"methods_removed": len(wanted)},
            )
    if token is None:
        raise refuse(400, "invalid_token")  # a parallel request consumed it
    await attempt.succeed()
    return Done()


@router.post("/password/change")
async def change_password(
    body: ChangePasswordIn,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_sensitive)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> Done:
    """Set or change the password. Every other session is revoked; the caller's stays."""
    if info is None:
        raise HTTPException(status_code=401, detail={"reason": "not_authenticated"})
    try:
        password = normalize_password(body.new_password)
    except PasswordRuleError:
        raise refuse(400, "password_rule") from None
    await reserve(request, [Bucket(PASSWORD_CHANGE, str(ctx.user_id))])
    password_hash = await request.app.state.password_hasher.hash(password)
    async with system_session(factory(request)) as db:
        await db.execute(
            update(users).where(users.c.id == ctx.user_id).values(password_hash=password_hash)
        )
        await revoke_user_sessions(db, ctx.user_id, except_id=info.id)
        await delete_outstanding_tokens(db, ctx.user_id)
        await record_event(db, "password_changed", user_id=ctx.user_id, ip=request_ip(request))
    return Done()
