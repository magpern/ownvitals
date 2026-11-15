"""``/api/auth/passkeys``: list, rename, remove and register passkeys (ADR 0011).

- Registration (``register/options``, ``register``) and removal need recent authentication and a
  step-up (``require_sensitive``, section 8); renaming needs a full session only.
- Sign-in and re-authentication are in ``passkey_signin_routes``.
- Every success and failure writes an ``auth_events`` row (section 9) holding kinds, row ids and
  the IP: never a challenge, credential id, public key, user handle or cookie.

Every query filters on the session's own user; another user's passkey id answers 404, the same
as an unknown one. When the public host has no relying-party id (an IP address), every route
answers 404 ``passkeys_unavailable``.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth import mails
from ownvitals.auth.deps import (
    current_session,
    request_ip,
    require_sensitive,
    require_user,
)
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import factory, mailer
from ownvitals.auth.passkey_ceremonies import finish_registration, registration_options
from ownvitals.auth.passkeys import (
    Passkey,
    PasskeyFailure,
    RelyingParty,
    list_passkeys,
    normalize_name,
    relying_party,
    remove_passkey,
    rename_passkey,
)
from ownvitals.auth.recipients import request_mail_locale
from ownvitals.auth.sessions import (
    SessionInfo,
)
from ownvitals.auth.tables import webauthn_credentials
from ownvitals.context import UserContext
from ownvitals.db import system_session
from ownvitals.mail import send_logged

router = APIRouter(prefix="/api/auth/passkeys", tags=["auth"])

FAILED = {"reason": "passkey_failed"}
_B64 = r"^[A-Za-z0-9_-]+={0,2}$"

# base64url fields with a bound each; the credential id is 1023 bytes at most (section 2).
CredentialId = Annotated[str, StringConstraints(min_length=1, max_length=1400, pattern=_B64)]
ClientData = Annotated[str, StringConstraints(min_length=1, max_length=4096, pattern=_B64)]
AttestationObject = Annotated[str, StringConstraints(min_length=1, max_length=16384, pattern=_B64)]
Short = Annotated[str, StringConstraints(min_length=1, max_length=2048, pattern=_B64)]
Handle = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=_B64)]
Transport = Annotated[str, StringConstraints(max_length=32)]


class PasskeyIn(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AttestationIn(PasskeyIn):
    clientDataJSON: ClientData
    attestationObject: AttestationObject
    transports: list[Transport] = Field(default_factory=list, max_length=8)


class RegistrationCredentialIn(PasskeyIn):
    id: CredentialId
    rawId: CredentialId
    type: Literal["public-key"]
    response: AttestationIn
    authenticatorAttachment: Literal["platform", "cross-platform"] | None = None


class RegisterIn(PasskeyIn):
    credential: RegistrationCredentialIn
    name: str | None = Field(default=None, max_length=256)


class RenameIn(PasskeyIn):
    name: str = Field(max_length=256)


class PasskeyOut(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None
    synced: bool  # backup-eligible: shown, never enforced (section 2)
    backed_up: bool
    suspended: bool


class PasskeysOut(BaseModel):
    passkeys: list[PasskeyOut]
    limit: int


def _out(passkey: Passkey) -> PasskeyOut:
    return PasskeyOut(
        id=passkey.id,
        name=passkey.name,
        created_at=passkey.created_at,
        last_used_at=passkey.last_used_at,
        synced=passkey.backup_eligible,
        backed_up=passkey.backup_state,
        suspended=passkey.suspended,
    )


def rp_or_404(request: Request) -> RelyingParty:
    rp = relying_party(request.app.state.settings)
    if rp is None:
        raise HTTPException(status_code=404, detail={"reason": "passkeys_unavailable"})
    return rp


def session_or_401(info: SessionInfo | None) -> SessionInfo:
    if info is None:
        raise HTTPException(status_code=401, detail={"reason": "not_authenticated"})
    return info


def _passkey_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(status_code=404) from None


def _name(raw: str | None) -> str:
    try:
        return normalize_name(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail={"reason": "name_invalid"}) from None


# --- list, rename, remove -------------------------------------------------------------------------


@router.get("")
async def get_passkeys(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> PasskeysOut:
    rp_or_404(request)
    async with system_session(factory(request)) as db:
        rows = await list_passkeys(db, ctx.user_id)
    limit = request.app.state.settings.passkeys_per_account
    return PasskeysOut(passkeys=[_out(row) for row in rows], limit=limit)


@router.patch("/{passkey_id}")
async def rename(
    passkey_id: str,
    body: RenameIn,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
) -> PasskeyOut:
    """A label only (section 8): a full session, CSRF and the user filter."""
    rp_or_404(request)
    target, name = _passkey_id(passkey_id), _name(body.name)
    async with system_session(factory(request)) as db:
        row = await rename_passkey(db, ctx.user_id, target, name)
        if row is not None:
            await record_event(
                db,
                "passkey_renamed",
                user_id=ctx.user_id,
                ip=request_ip(request),
                detail={"passkey": str(row.id)},
            )
    if row is None:
        raise HTTPException(status_code=404)
    return _out(row)


@router.delete("/{passkey_id}", status_code=204)
async def remove(
    passkey_id: str, request: Request, ctx: Annotated[UserContext, Depends(require_sensitive)]
) -> None:
    """Recent authentication and a step-up (section 8). 404 for another user's id."""
    rp_or_404(request)
    target = _passkey_id(passkey_id)
    async with system_session(factory(request)) as db:
        removed = await remove_passkey(db, ctx.user_id, target)
        if removed is not None:
            await record_event(
                db,
                "passkey_removed",
                user_id=ctx.user_id,
                ip=request_ip(request),
                detail={"passkey": str(target)},
            )
    if removed is None:
        raise HTTPException(status_code=404)


# --- registration ---------------------------------------------------------------------------------


async def _count(db: AsyncSession, user_id: UUID) -> int:
    rows = await db.execute(
        select(webauthn_credentials.c.id).where(webauthn_credentials.c.user_id == user_id)
    )
    return len(rows.all())


@router.post("/register/options")
async def register_options(
    request: Request,
    ctx: Annotated[UserContext, Depends(require_sensitive)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> dict[str, Any]:
    rp, session = rp_or_404(request), session_or_401(info)
    async with system_session(factory(request)) as db:
        if await _count(db, ctx.user_id) >= request.app.state.settings.passkeys_per_account:
            raise HTTPException(status_code=409, detail={"reason": "passkey_limit"})
        return await registration_options(db, rp, ctx.user_id, session.email, session.id)


@router.post("/register", status_code=201)
async def register(
    body: RegisterIn,
    request: Request,
    background: BackgroundTasks,
    ctx: Annotated[UserContext, Depends(require_sensitive)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> PasskeyOut:
    rp, session, name = rp_or_404(request), session_or_401(info), _name(body.name)
    ip = request_ip(request)
    credential = body.credential
    try:
        passkey = await finish_registration(
            factory(request),
            request.app.state.settings,
            rp,
            user_id=ctx.user_id,
            session_id=session.id,
            credential=credential.model_dump(exclude_none=True),
            client_data=credential.response.clientDataJSON,
            transports=credential.response.transports,
            name=name,
        )
    except PasskeyFailure as exc:
        async with system_session(factory(request)) as db:
            await record_event(
                db,
                "passkey_register_failed",
                user_id=ctx.user_id,
                ip=ip,
                detail={"reason": exc.reason},
            )
        if exc.reason == "limit":
            raise HTTPException(status_code=409, detail={"reason": "passkey_limit"}) from None
        raise HTTPException(status_code=400, detail=FAILED) from None
    async with system_session(factory(request)) as db:
        await record_event(
            db,
            "passkey_registered",
            user_id=ctx.user_id,
            ip=ip,
            detail={"passkey": str(passkey.id)},
        )
        locale = await request_mail_locale(db, request, ctx.user_id)
    notice = mails.passkey_added_notice(session.email, passkey.name, locale=locale)
    background.add_task(send_logged, mailer(request), notice)
    return _out(passkey)
