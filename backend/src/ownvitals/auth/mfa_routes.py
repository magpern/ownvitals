"""``/api/auth/mfa``: TOTP enrolment and removal, and recovery codes. The code checks that
make a session full, step it up or re-authenticate an admin (``/api/auth/mfa/verify``,
``/api/auth/mfa/stepup``, ``/api/auth/reauth/totp``) are in ``mfa_verify_routes``.

ADR 0003 sections 6 and 7, and ownvitals ADR 0003 sections 2 and 4: TOTP is mandatory, so a
session that has not passed it reaches only ``verify`` (with a confirmed TOTP) or the first
enrolment (without one); the gate in the middleware enforces that, and ``require_session`` lets
those routes see such a session. Replacing or removing the TOTP and new recovery codes need
step-up. Every code attempt first takes its rate-limit slots (5 per 5 minutes
per account and IP, section 9) and is refused above the limit before any code is checked. A
wrong or replayed code writes ``mfa_failed``; the fifth in an hour mails the user. Each success
rotates the session. Wrong codes answer 400 ``invalid_code`` (403 stays reserved for "you lack
the level": ``reauth_required``, ``stepup_required``, ``mfa_required``).
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth import mfa, totp
from ownvitals.auth.cookies import set_session_cookie
from ownvitals.auth.deps import (
    current_session,
    request_ip,
    require_session,
    require_stepup,
    require_user,
)
from ownvitals.auth.events import record_event
from ownvitals.auth.keys import Keys
from ownvitals.auth.notices import notify_mfa_failures, notify_totp_enrolled
from ownvitals.auth.ratelimit import RateLimited, RateLimiter, mfa_buckets
from ownvitals.auth.recipients import request_mail_locale
from ownvitals.auth.sessions import (
    SessionGone,
    SessionInfo,
    revoke_user_sessions,
    rotate_session,
)
from ownvitals.auth.userlock import lock_user_row
from ownvitals.context import UserContext
from ownvitals.crypto.keks import KeyRing
from ownvitals.db import system_session

router = APIRouter(prefix="/api/auth", tags=["mfa"])

CODE_MAX = 64
PENDING_MAX = 512

Info = Annotated[SessionInfo | None, Depends(current_session)]
AnySession = Annotated[SessionInfo, Depends(require_session)]


class CodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=CODE_MAX)


class ConfirmIn(CodeIn):
    pending: str = Field(min_length=1, max_length=PENDING_MAX)


class MfaStatus(BaseModel):
    totp_enabled: bool
    recovery_codes_left: int


class EnrollmentStart(BaseModel):
    secret: str  # the manual key (base32)
    otpauth_uri: str
    qr_svg: str
    pending: str  # sent back, with the first code, to confirm


class RecoveryCodes(BaseModel):
    recovery_codes: list[str]


def _refuse(status: int, reason: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"reason": reason})


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


def _keys(request: Request) -> Keys:
    return request.app.state.keys


def _ring(request: Request) -> KeyRing:
    return request.app.state.keyring


def _session(info: SessionInfo | None) -> SessionInfo:
    if info is None:  # unreachable behind require_user; keeps the type checker honest
        raise _refuse(401, "not_authenticated")
    return info


def _invalid_code(background: BackgroundTasks) -> JSONResponse:
    """400 ``invalid_code``. A response, not an exception, so that the mail queued in
    ``background`` still goes out (an ``HTTPException`` drops background tasks)."""
    return JSONResponse(
        {"detail": {"reason": "invalid_code"}}, status_code=400, background=background
    )


def _need_totp(info: SessionInfo) -> None:
    if not info.totp_enabled:
        raise _refuse(409, "totp_not_enabled")


def _may_enrol(info: SessionInfo) -> None:
    """Replacing a confirmed TOTP needs a full session and a step-up; a first enrolment (no
    factor to step up with) needs the fresh sign-in of the session, within ten minutes."""
    if info.totp_enabled:
        if not info.full:
            raise _refuse(403, "two_factor_required")
        if not info.recent_stepup:
            raise _refuse(403, "step_up_required")
    elif not (info.recent_auth or info.recent_stepup):
        raise _refuse(403, "reauth_required")


async def _reserve(request: Request, info: SessionInfo):
    """Take the MFA rate-limit slots of this attempt, or raise ``RateLimited`` (429)."""
    limiter: RateLimiter = request.app.state.limiter
    attempt = await limiter.begin(mfa_buckets(str(info.user_id), request_ip(request)))
    if not attempt.allowed:
        raise RateLimited(attempt.retry_after)
    return attempt


async def _rotate(request: Request, response: Response, info: SessionInfo, **flags: bool) -> None:
    """Rotate the session. Raises ``SessionGone`` when it was revoked meanwhile."""
    async with system_session(_factory(request)) as db:
        issued = await rotate_session(
            db,
            info,
            user_agent=request.headers.get("user-agent"),
            ip=request_ip(request),
            **flags,
        )
    set_session_cookie(response, issued)


@router.get("/mfa")
async def status(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)], info: Info
) -> MfaStatus:
    async with system_session(_factory(request)) as db:
        left = await mfa.recovery_codes_left(db, ctx.user_id)
    return MfaStatus(totp_enabled=_session(info).totp_enabled, recovery_codes_left=left)


@router.post("/mfa/totp/start")
async def start_enrollment(request: Request, session: AnySession) -> EnrollmentStart:
    """A first enrolment, or (with step-up) a replacement. Nothing is stored until
    ``confirm``."""
    _may_enrol(session)
    secret = totp.new_secret()
    uri = totp.provisioning_uri(secret, session.email)
    async with system_session(_factory(request)) as db:
        generation = await mfa.current_generation(db, session.user_id)
    return EnrollmentStart(
        secret=secret,
        otpauth_uri=uri,
        qr_svg=totp.qr_svg(uri),
        pending=mfa.seal_pending(_keys(request), session.user_id, secret, generation),
    )


@router.post("/mfa/totp/confirm", response_model=RecoveryCodes)
async def confirm_enrollment(
    body: ConfirmIn,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    session: AnySession,
) -> RecoveryCodes | JSONResponse:
    """One valid code for the new secret confirms it. Shows the recovery codes once, mails the
    user, and the session becomes full: it counts as having passed TOTP and step-up."""
    _may_enrol(session)
    user_id = session.user_id
    attempt = await _reserve(request, session)
    pending = mfa.open_pending(_keys(request), user_id, body.pending)
    codes: list[str] | None = None
    mail = False
    async with system_session(_factory(request)) as db:
        if pending is not None:
            secret, generation = pending
            # The step-up rule was applied for the TOTP state the session saw; the blob must
            # have been started against the same state.
            if (generation != mfa.NO_TOTP) != session.totp_enabled:
                raise _refuse(409, "totp_changed")
            try:
                codes = await mfa.confirm_enrollment(
                    db, _keys(request), _ring(request), user_id, secret, generation, body.code
                )
            except mfa.EnrollmentConflict:
                raise _refuse(409, "totp_changed") from None
        if codes is None:
            mail = await mfa.record_failure(db, user_id, request_ip(request), "enroll")
        else:
            kind = "totp_replaced" if session.totp_enabled else "totp_enrolled"
            await record_event(db, kind, user_id=user_id, ip=request_ip(request))
        locale = await request_mail_locale(db, request, user_id)  # for either notice
    if codes is None:
        if mail:
            background.add_task(notify_mfa_failures, user_id, session.email, locale)
        return _invalid_code(background)
    await attempt.succeed()
    # The enrollment is committed: mail the user now, whatever happens to the session.
    background.add_task(notify_totp_enrolled, user_id, session.email, locale)
    try:
        await _rotate(request, response, session, mfa=True, stepup=True)
    except SessionGone:
        # Revoked meanwhile. The user proved the factor, so the codes are not lost: they come
        # back in this body, and the client signs in again.
        return JSONResponse(
            {
                "detail": {"reason": "not_authenticated"},
                "enrolled": True,
                "recovery_codes": codes,
            },
            status_code=401,
            background=background,
        )
    return RecoveryCodes(recovery_codes=codes)


@router.delete("/mfa/totp", status_code=204)
async def remove_totp(
    request: Request,
    response: Response,
    ctx: Annotated[UserContext, Depends(require_stepup)],
    info: Info,
) -> None:
    """Remove (or reset) the TOTP: step-up first. Every other session is revoked, and this one
    is rotated without its second factor, so it goes straight to enrolment (ownvitals ADR 0003
    section 4: there is never a full session without TOTP)."""
    session = _session(info)
    _need_totp(session)
    async with system_session(_factory(request)) as db:
        await lock_user_row(db, ctx.user_id)  # first (``userlock``), then the TOTP row
        await mfa.remove_totp(db, ctx.user_id)
        await revoke_user_sessions(db, ctx.user_id, except_id=session.id)
        await record_event(db, "totp_removed", user_id=ctx.user_id, ip=request_ip(request))
        try:
            issued = await rotate_session(
                db,
                session,
                drop_mfa=True,
                user_agent=request.headers.get("user-agent"),
                ip=request_ip(request),
            )
        except SessionGone:
            raise _refuse(401, "not_authenticated") from None
    set_session_cookie(response, issued)


@router.post("/mfa/recovery-codes")
async def regenerate_recovery_codes(
    request: Request, ctx: Annotated[UserContext, Depends(require_stepup)], info: Info
) -> RecoveryCodes:
    _need_totp(_session(info))
    async with system_session(_factory(request)) as db:
        codes = await mfa.regenerate_recovery_codes(db, _keys(request), ctx.user_id)
        if codes is None:
            raise _refuse(409, "totp_not_enabled")
        await record_event(
            db, "recovery_codes_regenerated", user_id=ctx.user_id, ip=request_ip(request)
        )
    return RecoveryCodes(recovery_codes=codes)
