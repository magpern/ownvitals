"""``/api/auth/passkeys``: passkey sign-in (ADR 0011 sections 1, 3, 4).

- Sign-in (``signin/options``, ``signin``) is anonymous. The options issue a new pre-session
  cookie (section 4) and are limited to 20 per 10 minutes per IP; a failed assertion counts in
  the sign-in buckets per IP and, once the credential names an account, per account. Every
  failure answers the same 401 ``passkey_failed`` (T9, T86). A passkey sign-in gives a session
  that still owes its TOTP, like a password sign-in (section 3). A cross-device one
  (``passkeys.is_cross_device``: a phone over the hybrid transport, a security key, or a claim
  that doesn't fit the registered transports) gets the shorter session of
  ``session_cross_device``, no known-device cookie, and ``cross_device: true`` in its event
  (section 3, amendment of 2026-11-08).
- Re-authentication (``reauth/options``, ``reauth``, in ``passkey_reauth_routes``) sets
  ``reauth_at`` and rotates the session; it never counts as a step-up.
- Every success and failure writes an ``auth_events`` row (section 9) holding kinds, row ids and
  the IP: never a challenge, credential id, public key, user handle or cookie. A sign-count
  regression also writes ``passkey_suspended`` and mails the account.
"""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth import mails, presession
from ownvitals.auth.accounts import grant_admin_if_listed
from ownvitals.auth.clientip import bucket_ip
from ownvitals.auth.cookies import (
    clear_pre_cookie,
    set_device_cookie,
    set_pre_cookie,
    set_session_cookie,
)
from ownvitals.auth.deps import (
    request_device_token,
    request_ip,
    request_session_token,
)
from ownvitals.auth.devices import is_known_device, remember_device
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import factory, mailer, reserve, user_agent
from ownvitals.auth.passkey_ceremonies import (
    Assertion,
    StoredCredential,
    authenticate,
    signin_options,
)
from ownvitals.auth.passkey_routes import (
    FAILED,
    ClientData,
    CredentialId,
    Handle,
    PasskeyIn,
    Short,
    rp_or_404,
)
from ownvitals.auth.passkeys import PasskeyFailure, b64url_decode, is_cross_device
from ownvitals.auth.ratelimit import (
    PASSKEY_OPTIONS_IP,
    SIGN_IN_IP,
    Attempt,
    Bucket,
    password_buckets,
)
from ownvitals.auth.session_cross_device import mark_cross_device
from ownvitals.auth.sessions import (
    IssuedSession,
    start_session,
)
from ownvitals.auth.tables import users
from ownvitals.db import system_session
from ownvitals.mail import send_logged

router = APIRouter(prefix="/api/auth/passkeys", tags=["auth"])


class AssertionResponseIn(PasskeyIn):
    clientDataJSON: ClientData
    authenticatorData: Short
    signature: Short
    userHandle: Handle | None = None


class AssertionCredentialIn(PasskeyIn):
    id: CredentialId
    rawId: CredentialId
    type: Literal["public-key"]
    response: AssertionResponseIn
    authenticatorAttachment: Literal["platform", "cross-platform"] | None = None


class AssertionIn(PasskeyIn):
    credential: AssertionCredentialIn


class SignedIn(BaseModel):
    id: UUID
    email: str
    role: str


def _assertion(body: AssertionIn) -> Assertion | None:
    """The decoded parts, or ``None`` when ``id`` and ``rawId`` disagree or don't decode."""
    credential = body.credential
    try:
        raw_id = b64url_decode(credential.rawId)
        if b64url_decode(credential.id) != raw_id:
            return None
        handle = credential.response.userHandle
        user_handle = None if handle is None else b64url_decode(handle)
    except ValueError:
        return None
    return Assertion(
        credential=credential.model_dump(exclude_none=True),
        credential_id=raw_id,
        client_data=credential.response.clientDataJSON,
        user_handle=user_handle,
    )


async def _refused(
    request: Request,
    background: BackgroundTasks,
    exc: PasskeyFailure,
    kind: str,
    detail: dict[str, Any] | None = None,
) -> JSONResponse:
    """Record the refusal (and a suspension, with its mail) and answer the generic 401."""
    ip = request_ip(request)
    async with system_session(factory(request)) as db:
        await record_event(
            db, kind, user_id=exc.user_id, ip=ip, detail={"reason": exc.reason, **(detail or {})}
        )
        if exc.suspended is not None:
            await record_event(db, "passkey_suspended", user_id=exc.user_id, ip=ip)
    if exc.suspended is not None:
        name, email = exc.suspended
        background.add_task(
            send_logged, mailer(request), mails.passkey_suspended_notice(email, name)
        )
    return JSONResponse({"detail": FAILED}, status_code=401, background=background)


# --- sign-in --------------------------------------------------------------------------------------


@router.post("/signin/options")
async def signin_start(request: Request, response: Response) -> dict[str, Any]:
    """Discoverable sign-in options and a new pre-session cookie (the old one is replaced)."""
    rp = rp_or_404(request)
    await reserve(request, [Bucket(PASSKEY_OPTIONS_IP, bucket_ip(request_ip(request)))])
    async with system_session(factory(request)) as db:
        presession_id, token = await presession.issue(db, presession.request_pre_token(request))
        options = await signin_options(db, rp, presession_id)
    set_pre_cookie(response, token)
    return options


@router.post("/signin")
async def signin_finish(
    body: AssertionIn, request: Request, response: Response, background: BackgroundTasks
) -> Any:
    rp, ip = rp_or_404(request), request_ip(request)
    settings, keys = request.app.state.settings, request.app.state.keys
    device_token = request_device_token(request)
    pre_token = presession.request_pre_token(request)
    attempts: list[Attempt] = []

    async def gate(stored: StoredCredential | None) -> None:
        if stored is None:
            buckets = [Bucket(SIGN_IN_IP, bucket_ip(ip))]
        else:
            async with system_session(factory(request)) as db:
                known = await is_known_device(db, keys, stored.user_id, device_token)
            buckets = password_buckets(stored.email_normalized, ip, known_device=known)
        attempts.append(await reserve(request, buckets))

    async def finish(
        db: AsyncSession, stored: StoredCredential
    ) -> tuple[IssuedSession, str, SignedIn]:
        await grant_admin_if_listed(db, settings, stored.user_id)  # may revoke every session
        issued = await start_session(
            db,
            settings,
            stored.user_id,
            user_agent=user_agent(request),
            ip=ip,
            replacing_token=request_session_token(request),
        )
        detail: dict[str, Any] = {"passkey": str(stored.id)}
        device = ""
        if is_cross_device(body.credential.authenticatorAttachment, stored.transports):
            # Client-reported, so it only ever makes the session stricter: 24 hours absolute,
            # 1 hour idle, and the browser is not made a known device (ADR 0011 section 3).
            issued = await mark_cross_device(db, issued)
            detail["cross_device"] = True
        else:
            device = await remember_device(db, keys, stored.user_id, device_token)
        await presession.end(db, pre_token)
        await record_event(db, "passkey_signin", user_id=stored.user_id, ip=ip, detail=detail)
        role = (
            await db.execute(select(users.c.role).where(users.c.id == stored.user_id))
        ).scalar_one()
        return issued, device, SignedIn(id=stored.user_id, email=stored.email, role=role)

    assertion = _assertion(body)
    try:
        if assertion is None:
            await gate(None)
            raise PasskeyFailure("client_data")
        async with system_session(factory(request)) as db:
            presession_id = await presession.find(db, pre_token)
        issued, device, signed_in = await authenticate(
            factory(request),
            rp,
            assertion,
            purpose="signin",
            gate=gate,
            finish=finish,
            presession_id=presession_id,
        )
    except PasskeyFailure as exc:
        return await _refused(request, background, exc, "passkey_signin_failed")
    await attempts[0].succeed()
    set_session_cookie(response, issued)
    if device:  # none after a cross-device sign-in
        set_device_cookie(response, device)
    clear_pre_cookie(response)
    return signed_in
