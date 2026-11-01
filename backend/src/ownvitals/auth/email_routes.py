"""Email change and account deletion (ADR 0003 sections 5 and 6).

The change request needs recent authentication (and step-up with a confirmed TOTP) and answers
the same whether or not the new address is in use; only the mail differs. The confirmation needs
a signed-in session of the user the token was issued to, so a victim clicking a link from
someone else's account changes nothing. Deleting the account deletes the ``users`` row; the
foreign keys cascade.
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from ownvitals.auth import mails
from ownvitals.auth.accounts import find_user
from ownvitals.auth.clientip import bucket_ip
from ownvitals.auth.cookies import clear_session_cookie
from ownvitals.auth.deps import current_session, request_ip, require_recent_auth, require_user
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import EmailAddress, factory, mailer, reserve
from ownvitals.auth.ratelimit import MAIL_ACCOUNT, MAIL_IP, MAIL_PAIR, SIGN_IN_IP, Bucket
from ownvitals.auth.sessions import SessionInfo, revoke_user_sessions
from ownvitals.auth.signup_routes import Accepted, refuse
from ownvitals.auth.tables import users
from ownvitals.auth.tokens import (
    EMAIL_CHANGE_LIFETIME,
    consume_token,
    delete_outstanding_tokens,
    issue_token,
)
from ownvitals.context import UserContext
from ownvitals.db import system_session
from ownvitals.emailnorm import normalize_email
from ownvitals.mail import MailMessage, send_logged

router = APIRouter(prefix="/api/auth", tags=["auth"])

PURPOSE = "change_email"


class ChangeEmailIn(BaseModel):
    new_email: EmailAddress


class ConfirmEmailIn(BaseModel):
    token: str = Field(max_length=1024)


class _Refused(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


class Changed(BaseModel):
    status: str = "changed"
    email: str


@router.post("/email/change", status_code=202)
async def request_email_change(
    body: ChangeEmailIn,
    request: Request,
    background: BackgroundTasks,
    ctx: Annotated[UserContext, Depends(require_recent_auth)],
) -> Accepted:
    settings = request.app.state.settings
    normalized, ip = normalize_email(body.new_email), bucket_ip(request_ip(request))
    await reserve(
        request,
        [
            Bucket(MAIL_PAIR, f"{normalized}|{ip}"),
            Bucket(MAIL_ACCOUNT, normalized),
            Bucket(MAIL_ACCOUNT, f"user:{ctx.user_id}"),
            Bucket(MAIL_IP, ip),
        ],
    )
    message: MailMessage
    async with system_session(factory(request)) as db:
        current = (
            await db.execute(select(users.c.email).where(users.c.id == ctx.user_id))
        ).scalar_one()
        if await find_user(db, body.new_email) is not None:
            message = mails.email_change_taken(settings, body.new_email)  # nothing is stored
        else:
            token = await issue_token(
                db,
                PURPOSE,
                ctx.user_id,
                EMAIL_CHANGE_LIFETIME,
                new_email=body.new_email,
                email_at_issue=current,
            )
            message = mails.email_change_confirmation(settings, body.new_email, token)
        await record_event(
            db, "email_change_requested", user_id=ctx.user_id, ip=request_ip(request)
        )
    background.add_task(send_logged, mailer(request), message)
    return Accepted()


@router.post("/email/confirm")
async def confirm_email_change(
    body: ConfirmEmailIn,
    request: Request,
    background: BackgroundTasks,
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> Changed:
    """Only a session of the token's own user can confirm. Any other session gets the same
    answer as for an unknown token, and the token stays unused."""
    if info is None:
        raise HTTPException(status_code=401, detail={"reason": "not_authenticated"})
    try:
        async with system_session(factory(request)) as db:
            token = await consume_token(db, PURPOSE, body.token, user_id=ctx.user_id)
            if token is None or token.new_email is None:
                raise _Refused(400, "invalid_token")
            current = (
                await db.execute(select(users.c.email).where(users.c.id == ctx.user_id))
            ).scalar_one()
            if token.email_at_issue != current:
                raise _Refused(400, "invalid_token")  # the address changed since the request
            old_email, new_email = current, token.new_email
            moved = False
            try:
                async with db.begin_nested():
                    await db.execute(
                        update(users)
                        .where(users.c.id == ctx.user_id)
                        .values(email=new_email, email_normalized=normalize_email(new_email))
                    )
                    moved = True
            except IntegrityError:
                pass  # never logged: its DETAIL holds the address
            if not moved:
                raise _Refused(409, "email_taken")  # still free is rechecked by the unique key
            await revoke_user_sessions(db, ctx.user_id, except_id=info.id)
            await delete_outstanding_tokens(db, ctx.user_id)
            await record_event(db, "email_changed", user_id=ctx.user_id)
    except _Refused as exc:
        if exc.reason == "invalid_token":
            await reserve(request, [Bucket(SIGN_IN_IP, bucket_ip(request_ip(request)))])
        raise refuse(exc.status, exc.reason) from None
    background.add_task(send_logged, mailer(request), mails.email_changed_notice(old_email))
    return Changed(email=new_email)


@router.delete("/account", status_code=204)
async def delete_account(
    request: Request,
    response: Response,
    ctx: Annotated[UserContext, Depends(require_recent_auth)],
) -> None:
    """Delete the user (recent auth, and step-up with a confirmed TOTP); rows cascade."""
    async with system_session(factory(request)) as db:
        await record_event(db, "account_deleted", ip=request_ip(request))
        await db.execute(delete(users).where(users.c.id == ctx.user_id))
    clear_session_cookie(response)
