"""The hand-off for an unverified or missing provider email (ADR 0003 section 5).

The callback made a ``pending_signups`` row (provider, subject, invite, ``browser_hash``) and set
``__Host-ownvitals_pending``. Nothing the client sends can name a provider, subject or invite:
the email form takes only an address, and everything else comes from the row the cookie points
to. The confirmation link creates the account only in the browser that holds the cookie.
"""

import hmac
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update

from ownvitals.auth import mails
from ownvitals.auth.accounts import find_user
from ownvitals.auth.clientip import bucket_ip
from ownvitals.auth.cookies import PENDING_COOKIE, clear_pending_cookie
from ownvitals.auth.deps import request_ip
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import EmailAddress, factory, mailer, reserve
from ownvitals.auth.keys import new_token, token_hash
from ownvitals.auth.oauth_outcomes import Refused, create_provider_account
from ownvitals.auth.ratelimit import SIGNUP_ADDRESS, SIGNUP_IP, Bucket, password_buckets
from ownvitals.auth.tables import pending_signups
from ownvitals.db import system_session
from ownvitals.emailnorm import normalize_email
from ownvitals.mail import MailMessage, send_logged

router = APIRouter(prefix="/api/auth/oauth", tags=["auth"])

CONFIRMATION_LIFETIME = timedelta(hours=24)


class PendingState(BaseModel):
    pending: bool
    provider: str | None = None
    awaiting_email: bool = False


class PendingEmailIn(BaseModel):
    """Only an address. Any other field a client sends (provider, subject, invite) is ignored:
    pydantic drops unknown keys and nothing reads them."""

    email: EmailAddress


class Accepted(BaseModel):
    status: str = "check_your_inbox"


def _refuse(status: int, reason: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"reason": reason})


def _browser_row(cookie: str | None):
    if not cookie:
        return None
    return select(
        pending_signups.c.id,
        pending_signups.c.provider,
        pending_signups.c.email,
    ).where(
        pending_signups.c.browser_hash == token_hash(cookie),
        pending_signups.c.provider.is_not(None),
        pending_signups.c.expires_at > func.now(),
    )


@router.get("/pending")
async def pending_state(request: Request) -> PendingState:
    """What the email page shows: whether this browser has a pending provider sign-up."""
    query = _browser_row(request.cookies.get(PENDING_COOKIE))
    if query is None:
        return PendingState(pending=False)
    async with system_session(factory(request)) as db:
        row = (await db.execute(query)).first()
    if row is None:
        return PendingState(pending=False)
    return PendingState(pending=True, provider=row.provider, awaiting_email=row.email is None)


@router.post("/pending/email", status_code=202)
async def submit_email(
    body: PendingEmailIn, request: Request, background: BackgroundTasks
) -> Accepted:
    settings = request.app.state.settings
    normalized = normalize_email(body.email)
    await reserve(
        request,
        [Bucket(SIGNUP_ADDRESS, normalized), Bucket(SIGNUP_IP, bucket_ip(request_ip(request)))],
    )
    query = _browser_row(request.cookies.get(PENDING_COOKIE))
    message: MailMessage | None = None
    async with system_session(factory(request)) as db:
        row = None if query is None else (await db.execute(query)).first()
        if row is None:
            raise _refuse(400, "no_pending_signup")
        if row.email is not None:
            pass  # already filled in: same answer, nothing changes
        elif await find_user(db, body.email) is not None:
            # The row keeps no email or token and simply expires at its 15 minutes.
            message = mails.account_exists(settings, body.email)
        else:
            token = new_token()
            filled = await db.execute(
                update(pending_signups)
                .where(pending_signups.c.id == row.id, pending_signups.c.email.is_(None))
                .values(
                    email=body.email,
                    email_normalized=normalized,
                    token_hash=token_hash(token),
                    expires_at=func.now() + CONFIRMATION_LIFETIME,
                )
                .returning(pending_signups.c.id)
            )
            if filled.first() is not None:
                message = mails.provider_signup_confirmation(
                    settings, body.email, row.provider, token
                )
    if message is not None:
        background.add_task(send_logged, mailer(request), message)
    return Accepted()


async def confirm_provider_signup(
    request: Request,
    response: Response,
    background: BackgroundTasks,
    digest: bytes,
    row: object,
) -> None:
    """The confirmation of a provider row: only in the browser that holds the pending cookie.

    ``row`` has ``email_normalized`` and ``browser_hash``. In any other browser nothing is
    created, the token stays unused and the attempt counts as a sign-in failure.
    """
    ip = request_ip(request)
    email_normalized: str = row.email_normalized  # pyright: ignore[reportAttributeAccessIssue]
    browser_hash: bytes = row.browser_hash  # pyright: ignore[reportAttributeAccessIssue]
    attempt = await reserve(request, password_buckets(email_normalized, ip, known_device=False))
    cookie = request.cookies.get(PENDING_COOKIE)
    if not cookie or not hmac.compare_digest(token_hash(cookie), browser_hash):
        raise _refuse(400, "wrong_browser")  # finish on the device where the sign-in started
    await attempt.succeed()
    settings = request.app.state.settings
    try:
        async with system_session(factory(request)) as db:
            pending = (
                await db.execute(
                    delete(pending_signups)
                    .where(
                        pending_signups.c.token_hash == digest,
                        pending_signups.c.browser_hash == browser_hash,
                        pending_signups.c.expires_at > func.now(),
                    )
                    .returning(
                        pending_signups.c.email,
                        pending_signups.c.provider,
                        pending_signups.c.subject,
                        pending_signups.c.invite_id,
                    )
                )
            ).first()
            if pending is None:
                raise Refused("invalid_token")  # a parallel confirmation won
            user_id = await create_provider_account(
                db,
                settings,
                invite_id=pending.invite_id,
                email=pending.email,
                provider=pending.provider,
                subject=pending.subject,
            )
            await db.execute(
                delete(pending_signups).where(
                    pending_signups.c.email_normalized == email_normalized
                )
            )
            await record_event(db, "signup_confirmed", user_id=user_id, ip=ip)
    except Refused as exc:
        status = 400 if exc.code == "invalid_token" else 409
        raise _refuse(status, exc.code) from None
    clear_pending_cookie(response)
    background.add_task(
        send_logged, mailer(request), mails.identity_linked_notice(pending.email, pending.provider)
    )
