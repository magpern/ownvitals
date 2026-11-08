"""``/api/auth/mfa/verify``, ``/api/auth/mfa/stepup`` and ``/api/auth/reauth/totp``: a TOTP or
recovery code that makes the session full, steps it up or re-authenticates an admin.

The rules are those of ``mfa_routes`` (rate-limit slots before any code is checked, a wrong
code writes ``mfa_failed``, each success rotates the session), whose helpers this module uses.
"""

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import JSONResponse

from ownvitals.auth import mfa
from ownvitals.auth.deps import request_ip, require_user
from ownvitals.auth.events import record_event
from ownvitals.auth.mfa_routes import (
    AnySession,
    CodeIn,
    Info,
    _factory,
    _invalid_code,
    _keys,
    _need_totp,
    _refuse,
    _reserve,
    _ring,
    _rotate,
    _session,
)
from ownvitals.auth.notices import notify_mfa_failures
from ownvitals.auth.sessions import SessionGone, SessionInfo
from ownvitals.auth.totp_keys import TotpUnreadable
from ownvitals.context import UserContext
from ownvitals.db import system_session

router = APIRouter(prefix="/api/auth", tags=["mfa"])


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


async def _second_factor_endpoint(
    body: CodeIn,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    info: SessionInfo | None,
    purpose: str,
    event: str,
    **flags: bool,
) -> dict[str, Any] | JSONResponse:
    """Check the code, rotate the session with ``flags`` and write ``event`` (a failure writes
    ``mfa_failed`` with ``purpose``)."""
    session = _session(info)
    if (
        refused := await _verify_code(request, background, session, body.code, purpose)
    ) is not None:
        return refused
    try:
        await _rotate(request, response, session, **flags)
    except SessionGone:
        raise _refuse(401, "not_authenticated") from None
    async with system_session(_factory(request)) as db:
        await record_event(db, event, user_id=session.user_id, ip=request_ip(request))
    return {"ok": True}


@router.post("/mfa/verify", response_model=None)
async def verify(
    body: CodeIn,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    session: AnySession,
) -> dict[str, Any] | JSONResponse:
    """The second step after sign-in: TOTP (or recovery) code. Makes the session full, sets the
    12 h admin window and, as the same code, the step-up."""
    return await _second_factor_endpoint(
        body,
        request,
        response,
        background,
        session,
        "verify",
        "mfa_verified",
        mfa=True,
        stepup=True,
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
    """Step-up: a TOTP or recovery code, good for the step-up window (10 minutes)."""
    return await _second_factor_endpoint(
        body, request, response, background, info, "stepup", "step_up", stepup=True
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
        body,
        request,
        response,
        background,
        info,
        "reauth",
        "reauth_totp",
        reauth=True,
        mfa=True,
        stepup=True,
    )
