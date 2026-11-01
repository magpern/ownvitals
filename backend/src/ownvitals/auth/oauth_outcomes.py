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

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth import mails
from ownvitals.auth.accounts import create_user, find_user, grant_admin_if_listed, use_invite
from ownvitals.auth.devices import remember_device
from ownvitals.auth.events import record_event
from ownvitals.auth.keys import Keys, token_hash
from ownvitals.auth.oauth_client import ProviderIdentity
from ownvitals.auth.oauth_store import Flow, add_identity, identity_owner
from ownvitals.auth.sessions import (
    IssuedSession,
    SessionGone,
    SessionInfo,
    rotate_session,
    start_session,
)
from ownvitals.auth.tables import identities, invites, pending_signups
from ownvitals.config import Settings
from ownvitals.flags import effective as flags
from ownvitals.mail import MailMessage

REAUTH_MAX_AGE = 300  # seconds: auth_time must be within the last 5 minutes
AUTH_TIME_SKEW = 60
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


@dataclass(slots=True)
class Outcome:
    """``code`` is ``ok`` or the reason; the rest is what the route turns into cookies and mail."""

    code: str
    issued: IssuedSession | None = None
    device_token: str | None = None
    new_pending_token: str | None = None
    mail: MailMessage | None = None
    next: str = ""  # "signed_in", "linked", "reauthed" or "email_needed"


class Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> Outcome:
    return Outcome(code)


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
        return _fail("session_mismatch")
    ident, user_id = req.identity, info.user_id
    if await add_identity(
        db,
        user_id,
        req.flow.provider,
        ident.subject,
        ident.email if ident.email_verified else None,
    ):
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
    return _fail("identity_taken")  # on another user: refused


async def _reauth(db: AsyncSession, req: Request) -> Outcome:
    info = _own_session(req)
    if info is None:
        return _fail("session_mismatch")
    owner = await identity_owner(db, req.flow.provider, req.identity.subject)
    if owner is None or owner[0] != info.user_id:
        return _fail("identity_mismatch")
    if req.flow.provider != "github":
        # OIDC: a missing, old or future auth_time fails closed (the provider may ignore
        # max_age=0 or leave the claim out).
        now = time.time() if req.now is None else req.now
        auth_time = req.identity.auth_time
        if (
            auth_time is None
            or now - auth_time > REAUTH_MAX_AGE
            or auth_time - now > AUTH_TIME_SKEW
        ):
            await record_event(
                db, "reauth_failed", user_id=info.user_id, ip=req.ip, detail={"why": "auth_time"}
            )
            return _fail("reauth_stale")
    try:
        issued = await rotate_session(db, info, reauth=True, user_agent=req.user_agent, ip=req.ip)
    except SessionGone:
        return _fail("session_mismatch")
    await record_event(
        db, "reauth", user_id=info.user_id, ip=req.ip, detail={"provider": req.flow.provider}
    )
    return Outcome("ok", issued=issued, next="reauthed")


async def _invite_valid(db: AsyncSession, invite_id: UUID | None) -> bool:
    if invite_id is None:
        return False
    found = await db.execute(
        select(invites.c.id).where(
            invites.c.id == invite_id,
            invites.c.used_at.is_(None),
            invites.c.expires_at > func.now(),
        )
    )
    return found.first() is not None


async def _sign_in(db: AsyncSession, req: Request) -> Outcome:
    provider, ident, settings = req.flow.provider, req.identity, req.settings
    owner = await identity_owner(db, provider, ident.subject)
    if owner is not None:
        user_id, disabled = owner
        if disabled:
            await record_event(db, "sign_in_failed", user_id=user_id, ip=req.ip)
            return _fail("invalid")  # the generic failure
        return await _session_for(db, req, user_id)
    if ident.email_verified and ident.email and await find_user(db, ident.email) is not None:
        return _fail("account_exists")  # nothing is created or linked
    mode = await flags.signup_mode(db, settings.signup_mode)  # stricter of env and database
    gated = mode != "open"
    if mode == "closed" or (gated and req.flow.invite_id is None):
        return _fail("invite_required")
    if gated and not await _invite_valid(db, req.flow.invite_id):
        return _fail("invite_invalid")
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


async def consume_invite(db: AsyncSession, invite_id: UUID | None) -> None:
    """The conditional ``UPDATE`` of a provider sign-up. An invite the flow or hand-off carried
    is consumed whatever the sign-up mode is by then, so it cannot be used again. Raises
    ``Refused('invite_invalid')`` when the invite is gone; the caller's savepoint rolls
    everything back."""
    if invite_id is None:
        return
    if not await use_invite(db, invite_id):
        raise Refused("invite_invalid")


async def create_provider_account(
    db: AsyncSession,
    settings: Settings,
    *,
    invite_id: UUID | None,
    email: str,
    provider: str,
    subject: str,
) -> UUID:
    """One savepoint: consume the invite, create the user (verified) and the identity.
    Raises ``Refused`` with ``invite_invalid`` or ``account_exists``; nothing is left behind."""
    try:
        async with db.begin_nested():
            await consume_invite(db, invite_id)
            user_id = await create_user(db, email)
            await db.execute(
                insert(identities).values(
                    user_id=user_id, provider=provider, subject=subject, email_at_link=email
                )
            )
            if invite_id is not None:
                await db.execute(
                    update(invites).where(invites.c.id == invite_id).values(used_by=user_id)
                )
    except IntegrityError:
        raise Refused("account_exists") from None  # never chained: DETAIL holds the address
    return user_id


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
