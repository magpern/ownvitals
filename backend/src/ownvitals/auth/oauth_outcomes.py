"""What a verified provider identity does at the callback (ADR 0003 section 5, the table of
callback outcomes). Identities are keyed by ``(provider, subject)``, never by email (T1).

The flow purpose is evaluated first (``link``, ``reauth``), then the ``sign_in`` rows from the
top. Everything runs in the caller's transaction; the multi-step writes sit in a savepoint that
rolls back as a whole (for example when the invite is gone by the time the account is created).
"""

import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth import mails
from ownvitals.auth.accounts import find_user, grant_admin_if_listed
from ownvitals.auth.devices import remember_device
from ownvitals.auth.events import record_event
from ownvitals.auth.keys import Keys, token_hash
from ownvitals.auth.oauth_client import ProviderIdentity
from ownvitals.auth.oauth_reauth_policy import ReauthAnswer, refusal
from ownvitals.auth.oauth_signup import (  # noqa: F401 - re-exported
    Refused,
    consume_invite,
    create_provider_account,
    invite_valid,
)
from ownvitals.auth.oauth_store import Flow, add_identity, identity_owner
from ownvitals.auth.sessions import (
    IssuedSession,
    SessionGone,
    SessionInfo,
    rotate_session,
    start_session,
)
from ownvitals.auth.tables import pending_signups, users
from ownvitals.auth.userlock import lock_user_row
from ownvitals.config import Settings
from ownvitals.flags import effective as flags
from ownvitals.mail import MailMessage

HANDOFF_LIFETIME = timedelta(minutes=15)


@dataclass(slots=True)
class Request:
    """Everything an outcome needs besides the database."""

    settings: Settings
    keys: Keys
    flow: Flow
    identity: ProviderIdentity
    info: SessionInfo | None  # the request's current session
    session_token: str | None
    device_token: str | None
    pending_token: str | None  # the request's __Host-ownvitals_pending cookie
    ip: str
    user_agent: str | None
    now: float | None = None
    flow_age: float | None = None  # seconds since the flow's start, by the database clock


@dataclass(slots=True)
class Outcome:
    """``code`` is ``ok`` or the reason; the rest is what the route turns into cookies and mail."""

    code: str
    issued: IssuedSession | None = None
    device_token: str | None = None
    new_pending_token: str | None = None
    mail: MailMessage | None = None
    next: str = ""  # "signed_in", "linked", "reauthed" or "email_needed"
    why: str = ""  # a refusal's fixed word for the log line, never a claim value


def _fail(code: str, why: str = "") -> Outcome:
    return Outcome(code, why=why or code)


async def apply(db: AsyncSession, req: Request) -> Outcome:
    if req.flow.purpose == "link":
        return await _link(db, req)
    if req.flow.purpose == "reauth":
        return await _reauth(db, req)
    return await _sign_in(db, req)


def _own_session(req: Request) -> SessionInfo | None:
    """The request's session, only when it belongs to the flow's user (link and reauth)."""
    if req.info is None or req.flow.user_id is None or req.info.user_id != req.flow.user_id:
        return None
    return req.info


async def _link(db: AsyncSession, req: Request) -> Outcome:
    info = _own_session(req)
    if info is None:
        return _fail("session_mismatch", "not_the_flows_user")
    ident, user_id = req.identity, info.user_id
    try:
        linked = await add_identity(
            db,
            user_id,
            req.flow.provider,
            ident.subject,
            ident.email if ident.email_verified else None,
            session_id=info.id,
        )
    except SessionGone:  # a reset or revoke committed since the session was read (#248)
        return _fail("session_mismatch")
    if linked:
        await record_event(
            db,
            "identity_linked",
            user_id=user_id,
            ip=req.ip,
            detail={"provider": req.flow.provider},
        )
        mail = mails.identity_linked_notice(info.email, req.flow.provider)
        return Outcome("ok", mail=mail, next="linked")
    owner = await identity_owner(db, req.flow.provider, ident.subject)
    if owner is not None and owner[0] == user_id:
        return Outcome("ok", next="linked")  # already linked to this very account
    return _fail("identity_taken", "linked_to_another_user")


async def has_password(db: AsyncSession, user_id: UUID, *, lock: bool = False) -> bool | None:
    """``None`` when the account is gone. ``lock``: ``FOR UPDATE``, which a password change's
    ``UPDATE`` waits for, so a password set between the start and the callback is seen."""
    statement = select(users.c.password_hash.is_not(None)).where(users.c.id == user_id)
    if lock:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def _reauth(db: AsyncSession, req: Request) -> Outcome:
    info = _own_session(req)
    if info is None:
        return _fail("session_mismatch", "not_the_flows_user")
    password = await has_password(db, info.user_id, lock=True)  # first: see a change meanwhile
    if password is None:
        return _fail("session_mismatch", "account_gone")
    owner = await identity_owner(db, req.flow.provider, req.identity.subject)
    if owner is None or owner[0] != info.user_id:
        return _fail("identity_mismatch", "not_linked_to_this_user")
    refused = refusal(
        ReauthAnswer(
            provider=req.flow.provider,
            has_password=password,
            flow_age=req.flow_age,
            email_verified=req.identity.email_verified,
            auth_time=req.identity.auth_time,
            now=time.time() if req.now is None else req.now,
        )
    )
    if refused is not None:
        code, why = refused
        await record_event(
            db,
            "reauth_failed",
            user_id=info.user_id,
            ip=req.ip,
            detail={"why": why, "provider": req.flow.provider},
        )
        return _fail(code, why)
    try:
        issued = await rotate_session(db, info, reauth=True, user_agent=req.user_agent, ip=req.ip)
    except SessionGone:
        return _fail("session_mismatch", "session_gone")
    await record_event(
        db, "reauth", user_id=info.user_id, ip=req.ip, detail={"provider": req.flow.provider}
    )
    return Outcome("ok", issued=issued, next="reauthed")


async def _sign_in(db: AsyncSession, req: Request) -> Outcome:
    provider, ident, settings = req.flow.provider, req.identity, req.settings
    owner = await identity_owner(db, provider, ident.subject)
    if owner is not None:
        user_id = owner[0]
        # The user row first (``userlock``), then the identity and the account again: a reset
        # that removed the identity, or a disable or delete, committed since the read above.
        locked = await lock_user_row(db, user_id)
        again = await identity_owner(db, provider, ident.subject)
        if locked is None or again is None or again[0] != user_id:
            return _fail("invalid")
        if locked.disabled:
            await record_event(db, "sign_in_failed", user_id=user_id, ip=req.ip)
            return _fail("invalid", "user_disabled")  # the generic failure
        return await _session_for(db, req, user_id)
    if ident.email_verified and ident.email and await find_user(db, ident.email) is not None:
        return _fail("account_exists", "email_taken")  # nothing is created or linked
    # ``invite`` or ``closed``: ``open`` is refused until the M8 gate (ADR 0003 section 6).
    mode = await flags.signup_mode(db, settings.signup_mode)
    if mode == "closed" or req.flow.invite_id is None:
        return _fail("invite_required", "signup_closed" if mode == "closed" else "no_invite")
    if not await invite_valid(db, req.flow.invite_id):
        return _fail("invite_invalid", "invite_gone")
    if ident.email_verified and ident.email:
        return await _create_account(db, req, ident.email)
    return await _hand_off(db, req)


async def _session_for(db: AsyncSession, req: Request, user_id: UUID) -> Outcome:
    await grant_admin_if_listed(db, req.settings, user_id)  # may revoke every session
    issued = await start_session(
        db,
        req.settings,
        user_id,
        user_agent=req.user_agent,
        ip=req.ip,
        replacing_token=req.session_token,
    )
    device = await remember_device(db, req.keys, user_id, req.device_token)
    await record_event(
        db, "sign_in", user_id=user_id, ip=req.ip, detail={"provider": req.flow.provider}
    )
    return Outcome("ok", issued=issued, device_token=device, next="signed_in")


async def _create_account(db: AsyncSession, req: Request, email: str) -> Outcome:
    try:
        user_id = await create_provider_account(
            db,
            req.settings,
            invite_id=req.flow.invite_id,
            email=email,
            provider=req.flow.provider,
            subject=req.identity.subject,
        )
    except Refused as exc:
        return _fail(exc.code)
    await record_event(db, "signup_confirmed", user_id=user_id, ip=req.ip)
    outcome = await _session_for(db, req, user_id)
    outcome.mail = mails.identity_linked_notice(email, req.flow.provider)
    return outcome


async def _hand_off(db: AsyncSession, req: Request) -> Outcome:
    """Unverified or missing provider email: a server-side row bound to this browser."""
    if req.pending_token:
        # A second pending sign-up in the same browser replaces the first.
        await db.execute(
            delete(pending_signups).where(
                pending_signups.c.browser_hash == token_hash(req.pending_token),
                pending_signups.c.provider.is_not(None),
            )
        )
    browser = secrets.token_urlsafe(32)
    await db.execute(
        insert(pending_signups).values(
            provider=req.flow.provider,
            subject=req.identity.subject,
            browser_hash=token_hash(browser),
            invite_id=req.flow.invite_id,
            expires_at=func.now() + HANDOFF_LIFETIME,
        )
    )
    return Outcome("ok", new_pending_token=browser, next="email_needed")
