"""``/api/auth/mfa`` and ``/api/auth/reauth/totp``: TOTP, step-up and recovery codes.

ADR 0003 sections 6 and 7. Every code attempt first takes its rate-limit slots (5 per 5 minutes
per account and IP, section 9) and is refused above the limit before any code is checked. A
wrong or replayed code writes ``mfa_failed``; the fifth in an hour mails the user. Each success
rotates the session. Wrong codes answer 400 ``invalid_code`` (403 stays reserved for "you lack
the level": ``reauth_required``, ``stepup_required``, ``mfa_required``).
"""

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth import mfa, totp
from ownvitals.auth.cookies import set_session_cookie
from ownvitals.auth.deps import (
    current_session,
    request_ip,
    require_recent_auth,
    require_stepup,
    require_user,
)
from ownvitals.auth.events import record_event
from ownvitals.auth.keys import Keys
from ownvitals.auth.notices import notify_mfa_failures, notify_totp_enrolled
from ownvitals.auth.ratelimit import RateLimited, RateLimiter, mfa_buckets
from ownvitals.auth.sessions import (
    SessionGone,
    SessionInfo,
    clear_mfa,
    revoke_user_sessions,
    rotate_session,
)
from ownvitals.auth.totp_keys import TotpUnreadable
from ownvitals.context import UserContext
from ownvitals.crypto.keks import KeyRing
from ownvitals.db import system_session

router = APIRouter(prefix="/api/auth", tags=["mfa"])

CODE_MAX = 64
PENDING_MAX = 512

Info = Annotated[SessionInfo | None, Depends(current_session)]


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


async def _verify_code(
    request: Request, background: BackgroundTasks, info: SessionInfo, code: str, purpose: str
) -> JSONResponse | None:
    """A TOTP or recovery code, rate limited. The 400 response for a bad one, else ``None``."""
    _need_totp(info)
    attempt = await _reserve(request, info)
    unreadable: TotpUnreadable | None = None
    async with system_session(_factory(request)) as db:
        try:
            method = await mfa.check_second_factor(
                db, _keys(request), _ring(request), info.user_id, code
            )
        except TotpUnreadable as error:
            unreadable, method = error, None
        mail = False
        if unreadable is not None:
            # Fails closed with the generic error; not an mfa_failed (ADR 0004 section 2).
            await record_event(db, unreadable.kind, user_id=info.user_id, ip=request_ip(request))
        elif method is None:
            mail = await mfa.record_failure(db, info.user_id, request_ip(request), purpose)
    if method is None:
        if mail:
            background.add_task(notify_mfa_failures, info.user_id, info.email)
        return _invalid_code(background)
    await attempt.succeed()
    return None


@router.get("/mfa")
async def status(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)], info: Info
) -> MfaStatus:
    async with system_session(_factory(request)) as db:
        left = await mfa.recovery_codes_left(db, ctx.user_id)
    return MfaStatus(totp_enabled=_session(info).totp_enabled, recovery_codes_left=left)


@router.post("/mfa/totp/start")
async def start_enrollment(
    request: Request, ctx: Annotated[UserContext, Depends(require_recent_auth)], info: Info
) -> EnrollmentStart:
    """Recent auth (and step-up when a TOTP already exists, which this would replace). Nothing
    is stored until ``confirm``."""
    secret = totp.new_secret()
    uri = totp.provisioning_uri(secret, _session(info).email)
    async with system_session(_factory(request)) as db:
        generation = await mfa.current_generation(db, ctx.user_id)
    return EnrollmentStart(
        secret=secret,
        otpauth_uri=uri,
        qr_svg=totp.qr_svg(uri),
        pending=mfa.seal_pending(_keys(request), ctx.user_id, secret, generation),
    )


@router.post("/mfa/totp/confirm", response_model=RecoveryCodes)
async def confirm_enrollment(
    body: ConfirmIn,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    ctx: Annotated[UserContext, Depends(require_recent_auth)],
    info: Info,
) -> RecoveryCodes | JSONResponse:
    """One valid code for the new secret confirms it. Shows the recovery codes once, mails the
    user, and the session counts as having passed TOTP and step-up."""
    session = _session(info)
    attempt = await _reserve(request, session)
    pending = mfa.open_pending(_keys(request), ctx.user_id, body.pending)
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
                    db, _keys(request), _ring(request), ctx.user_id, secret, generation, body.code
                )
            except mfa.EnrollmentConflict:
                raise _refuse(409, "totp_changed") from None
        if codes is None:
            mail = await mfa.record_failure(db, ctx.user_id, request_ip(request), "enroll")
        else:
            kind = "totp_replaced" if session.totp_enabled else "totp_enrolled"
            await record_event(db, kind, user_id=ctx.user_id, ip=request_ip(request))
    if codes is None:
        if mail:
            background.add_task(notify_mfa_failures, session.user_id, session.email)
        return _invalid_code(background)
    await attempt.succeed()
    # The enrollment is committed: mail the user now, whatever happens to the session.
    background.add_task(notify_totp_enrolled, session.user_id, session.email)
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
    request: Request, ctx: Annotated[UserContext, Depends(require_stepup)], info: Info
) -> None:
    """Removing TOTP needs step-up: a password-only re-auth is not enough."""
    session = _session(info)
    _need_totp(session)
    async with system_session(_factory(request)) as db:
        await mfa.remove_totp(db, ctx.user_id)
        await clear_mfa(db, session.id)
        await revoke_user_sessions(db, ctx.user_id, except_id=session.id)
        await record_event(db, "totp_removed", user_id=ctx.user_id, ip=request_ip(request))


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


async def _second_factor_endpoint(
    body: CodeIn,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    info: SessionInfo | None,
    purpose: str,
    **flags: bool,
) -> dict[str, Any] | JSONResponse:
    session = _session(info)
    if (
        refused := await _verify_code(request, background, session, body.code, purpose)
    ) is not None:
        return refused
    try:
        await _rotate(request, response, session, **flags)
    except SessionGone:
        raise _refuse(401, "not_authenticated") from None
    return {"ok": True}


@router.post("/mfa/verify", response_model=None)
async def verify(
    body: CodeIn,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    _: Annotated[UserContext, Depends(require_user)],
    info: Info,
) -> dict[str, Any] | JSONResponse:
    """The second step after sign-in: TOTP (or recovery) code. Sets the 12 h admin window and,
    as the same code, the step-up."""
    return await _second_factor_endpoint(
        body, request, response, background, info, "verify", mfa=True, stepup=True
    )


@router.post("/mfa/stepup", response_model=None)
async def stepup(
    body: CodeIn,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    _: Annotated[UserContext, Depends(require_user)],
    info: Info,
) -> dict[str, Any] | JSONResponse:
    """Step-up: a TOTP or recovery code, good for 5 minutes."""
    return await _second_factor_endpoint(
        body, request, response, background, info, "stepup", stepup=True
    )


@router.post("/reauth/totp", response_model=None)
async def reauth_with_totp(
    body: CodeIn,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    _: Annotated[UserContext, Depends(require_user)],
    info: Info,
) -> dict[str, Any] | JSONResponse:
    """Re-authentication by TOTP, for admins only (ADR 0003 section 6). One code sets
    ``reauth_at``, ``mfa_at`` and ``stepup_at``."""
    if _session(info).stored_role != "admin":
        raise _refuse(403, "forbidden")
    return await _second_factor_endpoint(
        body, request, response, background, info, "reauth", reauth=True, mfa=True, stepup=True
    )
