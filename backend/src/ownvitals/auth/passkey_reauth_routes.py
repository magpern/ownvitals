"""``/api/auth/passkeys/reauth``: re-authentication by passkey (ADR 0011 sections 1 and 3).

``reauth/options`` and ``reauth`` set ``reauth_at`` and rotate the session; a passkey never
counts as a step-up. The rules, events and refusals are those of ``passkey_signin_routes``,
whose helpers this module uses; ``setup`` includes this router right after that one.
"""

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.cookies import clear_session_cookie, set_session_cookie
from ownvitals.auth.deps import current_session, request_device_token, request_ip, require_user
from ownvitals.auth.devices import is_known_device
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import factory, reserve, user_agent
from ownvitals.auth.passkey_ceremonies import StoredCredential, authenticate, reauth_options
from ownvitals.auth.passkey_routes import rp_or_404, session_or_401
from ownvitals.auth.passkey_signin_routes import AssertionIn, _assertion, _refused
from ownvitals.auth.passkeys import PasskeyFailure
from ownvitals.auth.ratelimit import Attempt, password_buckets
from ownvitals.auth.sessions import IssuedSession, SessionGone, SessionInfo, rotate_session
from ownvitals.auth.stepup_first_factor import record_stepup, steps_up
from ownvitals.context import UserContext
from ownvitals.db import system_session
from ownvitals.emailnorm import normalize_email

router = APIRouter(prefix="/api/auth/passkeys", tags=["auth"])


class Done(BaseModel):
    status: str = "ok"


@router.post("/reauth/options")
async def reauth_start(
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> dict[str, Any]:
    rp, session = rp_or_404(request), session_or_401(info)
    async with system_session(factory(request)) as db:
        options = await reauth_options(db, rp, ctx.user_id, session.id)
    if options is None:
        raise HTTPException(status_code=404, detail={"reason": "no_passkeys"})
    return options


@router.post("/reauth")
async def reauth_finish(
    body: AssertionIn,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> Any:
    """Recent authentication by passkey (section 3): ``reauth_at``. ``stepup_at`` too only for
    an account without TOTP that is not required to have one (#475, ``stepup_first_factor``);
    the ceremony holds the user-row lock by then (``passkeys.lock_user``)."""
    rp, session, ip = rp_or_404(request), session_or_401(info), request_ip(request)
    keys = request.app.state.keys
    attempts: list[Attempt] = []

    async def gate(_: StoredCredential | None) -> None:
        async with system_session(factory(request)) as db:
            known = await is_known_device(db, keys, ctx.user_id, request_device_token(request))
        email = normalize_email(session.email)
        attempts.append(await reserve(request, password_buckets(email, ip, known_device=known)))

    async def finish(db: AsyncSession, stored: StoredCredential) -> IssuedSession:
        stepup = await steps_up(db, request.app.state.settings, ctx.user_id)
        issued = await rotate_session(
            db, session, reauth=True, stepup=stepup, user_agent=user_agent(request), ip=ip
        )
        await record_event(
            db, "passkey_reauth", user_id=ctx.user_id, ip=ip, detail={"passkey": str(stored.id)}
        )
        if stepup:
            await record_stepup(db, ctx.user_id, ip, "passkey")
        return issued

    assertion = _assertion(body)
    try:
        if assertion is None:
            await gate(None)
            raise PasskeyFailure("client_data", user_id=ctx.user_id)
        issued = await authenticate(
            factory(request),
            rp,
            assertion,
            purpose="reauth",
            gate=gate,
            finish=finish,
            session_id=session.id,
            session_user=ctx.user_id,
        )
    except PasskeyFailure as exc:
        return await _refused(
            request, background, exc, "passkey_signin_failed", {"purpose": "reauth"}
        )
    except SessionGone:
        clear_session_cookie(response)
        raise HTTPException(status_code=401, detail={"reason": "not_authenticated"}) from None
    await attempts[0].succeed()
    set_session_cookie(response, issued)
    return Done()
