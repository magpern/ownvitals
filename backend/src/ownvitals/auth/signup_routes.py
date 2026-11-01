"""Sign-up with a password through pending sign-ups (ADR 0003 section 5).

No account exists until the email is proven, and the invite is only consumed at confirmation.
``POST /api/auth/signup`` answers the same for a new and an existing address (the mail differs)
and sends one mail after the response. ``POST /api/auth/signup/confirm`` needs the token and the
chosen password; a wrong password leaves the row untouched and counts as a sign-in failure.

Provider sign-ups (``pending_signups.provider`` set) are confirmed in the browser that started
them (``oauth_handoff``); in any other browser the row and its token stay untouched.
"""

from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth import mails
from ownvitals.auth.accounts import create_user, find_user, use_invite
from ownvitals.auth.clientip import bucket_ip
from ownvitals.auth.deps import request_ip
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import EmailAddress, factory, mailer, reserve
from ownvitals.auth.keys import new_token, token_hash
from ownvitals.auth.oauth_handoff import confirm_provider_signup
from ownvitals.auth.passwords import PasswordRuleError, normalize_password
from ownvitals.auth.ratelimit import SIGN_IN_IP, SIGNUP_ADDRESS, SIGNUP_IP, Bucket, password_buckets
from ownvitals.auth.tables import invites, pending_signups
from ownvitals.db import system_session
from ownvitals.emailnorm import normalize_email
from ownvitals.flags import effective as flags
from ownvitals.mail import MailMessage, send_logged

router = APIRouter(prefix="/api/auth", tags=["auth"])

CONFIRMATION_LIFETIME = timedelta(hours=24)
FIELD_MAX = 1024


class SignUpIn(BaseModel):
    email: EmailAddress
    password: str = Field(max_length=FIELD_MAX)
    invite_code: str | None = Field(default=None, max_length=FIELD_MAX)


class ConfirmIn(BaseModel):
    token: str = Field(max_length=FIELD_MAX)
    # Required for a password sign-up; a provider sign-up is bound to its browser instead.
    password: str | None = Field(default=None, max_length=FIELD_MAX)


class Accepted(BaseModel):
    status: str = "check_your_inbox"


class Created(BaseModel):
    status: str = "created"


def refuse(status: int, reason: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"reason": reason})


async def valid_invite(db: AsyncSession, code: str | None) -> UUID | None:
    if not code:
        return None
    return (
        await db.execute(
            select(invites.c.id).where(
                invites.c.code_hash == token_hash(code.strip()),
                invites.c.used_at.is_(None),
                invites.c.expires_at > func.now(),
            )
        )
    ).scalar_one_or_none()


@router.post("/signup", status_code=202)
async def sign_up(body: SignUpIn, request: Request, background: BackgroundTasks) -> Accepted:
    settings = request.app.state.settings
    try:
        password = normalize_password(body.password)
    except PasswordRuleError:
        raise refuse(400, "password_rule") from None
    normalized = normalize_email(body.email)
    # Limited per normalized address and per IP; every request counts, known address or not.
    await reserve(
        request,
        [Bucket(SIGNUP_ADDRESS, normalized), Bucket(SIGNUP_IP, bucket_ip(request_ip(request)))],
    )
    async with system_session(factory(request)) as db:
        mode = await flags.signup_mode(db, settings.signup_mode)  # stricter of env and database
    if mode == "closed":
        raise refuse(403, "signups_closed")
    invite_id = None
    if mode == "invite":
        # Checked before hashing, so a bad invite never occupies the hashing pool.
        async with system_session(factory(request)) as db:
            invite_id = await valid_invite(db, body.invite_code)
        if invite_id is None:
            raise refuse(400, "invalid_invite")  # its own error (section 5)
    password_hash = await request.app.state.password_hasher.hash(password)  # both branches
    message: MailMessage
    async with system_session(factory(request)) as db:
        if await find_user(db, body.email) is not None:
            message = mails.account_exists(settings, body.email)  # nothing is stored
        else:
            token = new_token()
            await db.execute(
                insert(pending_signups).values(
                    token_hash=token_hash(token),
                    email=body.email,
                    email_normalized=normalized,
                    password_hash=password_hash,
                    invite_id=invite_id,
                    expires_at=func.now() + CONFIRMATION_LIFETIME,
                )
            )
            message = mails.signup_confirmation(settings, body.email, token)
    background.add_task(send_logged, mailer(request), message)
    return Accepted()


class _Refused(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


@router.post("/signup/confirm")
async def confirm_sign_up(
    body: ConfirmIn, request: Request, response: Response, background: BackgroundTasks
) -> Created:
    # While the effective sign-up mode is "closed" (the emergency brake, ADR 0011 section 7,
    # amendment 2026-10-31) no pending sign-up is confirmed, password or provider: 403
    # signups_closed. The pending row and its invite stay, so it can still be confirmed if sign-ups
    # reopen before it expires. "invite" and "open" do not matter here: the row was admitted when
    # it was made, and its invite is consumed at confirmation.
    ip = request_ip(request)
    digest = token_hash(body.token)
    async with system_session(factory(request)) as db:
        row = (
            await db.execute(
                select(
                    pending_signups.c.id,
                    pending_signups.c.email_normalized,
                    pending_signups.c.password_hash,
                    pending_signups.c.provider,
                    pending_signups.c.browser_hash,
                ).where(
                    pending_signups.c.token_hash == digest,
                    pending_signups.c.expires_at > func.now(),
                )
            )
        ).first()
    if row is None or (row.provider is None and row.password_hash is None):
        # Unknown or expired token: only the IP is counted (no account to name).
        await reserve(request, [Bucket(SIGN_IN_IP, bucket_ip(ip))])
        raise refuse(400, "invalid_token")
    async with system_session(factory(request)) as db:
        if await flags.signup_mode(db, request.app.state.settings.signup_mode) == "closed":
            raise refuse(403, "signups_closed")
    if row.provider is not None:
        await confirm_provider_signup(request, response, background, digest, row)
        return Created()
    attempt = await reserve(request, password_buckets(row.email_normalized, ip, known_device=False))
    try:
        typed = normalize_password(body.password) if body.password is not None else None
    except PasswordRuleError:
        typed = None
    if typed is None or not await request.app.state.password_hasher.verify(
        row.password_hash, typed
    ):
        raise refuse(400, "wrong_password")  # the row and the token stay; the slot stays taken
    await attempt.succeed()
    try:
        async with system_session(factory(request)) as db:
            await _create_account(db, digest, ip)
    except _Refused as exc:
        raise refuse(exc.status, exc.reason) from None
    return Created()


async def _create_account(db: AsyncSession, digest: bytes, ip: str) -> None:
    """One transaction: consume the pending row, consume the invite, create the user."""
    pending = (
        await db.execute(
            delete(pending_signups)
            .where(
                pending_signups.c.token_hash == digest, pending_signups.c.expires_at > func.now()
            )
            .returning(
                pending_signups.c.email,
                pending_signups.c.email_normalized,
                pending_signups.c.password_hash,
                pending_signups.c.invite_id,
            )
        )
    ).first()
    if pending is None:
        raise _Refused(400, "invalid_token")  # a parallel confirmation won
    if pending.invite_id is not None and not await use_invite(db, pending.invite_id):
        raise _Refused(409, "invite_invalid")  # rolls back: the pending row stays
    created = None
    try:
        async with db.begin_nested():
            created = await create_user(db, pending.email, password_hash=pending.password_hash)
    except IntegrityError:
        pass  # never logged or chained: its DETAIL can hold the address
    if created is None:
        raise _Refused(409, "account_exists")
    if pending.invite_id is not None:
        await db.execute(
            update(invites).where(invites.c.id == pending.invite_id).values(used_by=created)
        )
    await db.execute(
        delete(pending_signups).where(
            pending_signups.c.email_normalized == pending.email_normalized
        )
    )
    await record_event(db, "signup_confirmed", user_id=created, ip=ip)
