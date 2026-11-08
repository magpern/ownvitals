"""Google sign-in, linking and re-authentication (ADR 0003 sections 1 and 6).

``POST /api/auth/oauth/{provider}/start`` (CSRF-checked) creates the single-use flow and returns
the authorization URL. ``GET /oauth/{provider}/callback`` takes the flow atomically *before* the
code is exchanged, checks provider and state, and answers with a redirect: the outcome is a short
code in the query of an SPA page, never anything the provider sent. This route is registered
before the SPA catch-all; its query string (code, state) is never logged. Every callback that
does not end ``ok`` writes one ``WARNING`` line with the provider, the purpose, the outcome code
and a fixed ``why`` word (#247): never a code, state, nonce, token, email or claim value.

A Google ``reauth`` of an account with a password is refused at the start (403
``reauth_use_password``, no flow) and again at the callback under the row lock
(``oauth_reauth_policy``, ADR 0003 section 1, amendment of 2026-11-08).
"""

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.clientip import bucket_ip
from ownvitals.auth.cookies import (
    OAUTH_COOKIE,
    PENDING_COOKIE,
    clear_oauth_cookie,
    set_device_cookie,
    set_oauth_cookie,
    set_pending_cookie,
    set_session_cookie,
)
from ownvitals.auth.deps import (
    current_context,
    current_session,
    request_device_token,
    request_ip,
    request_session_token,
    require_sensitive,
    require_user,
)
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import factory, mailer, reserve, user_agent
from ownvitals.auth.keys import token_hash
from ownvitals.auth.locale import for_new_account
from ownvitals.auth.oauth_client import OAuthRuntime, ProviderError, authorization_url, identify
from ownvitals.auth.oauth_errors import OAUTH_ERRORS
from ownvitals.auth.oauth_outcomes import Outcome, apply, has_password
from ownvitals.auth.oauth_outcomes import Request as OutcomeRequest
from ownvitals.auth.oauth_store import Flow, create_flow, safe_return_to, take_flow
from ownvitals.auth.ratelimit import OAUTH_IP, Bucket
from ownvitals.auth.sessions import SessionInfo
from ownvitals.auth.signup_routes import valid_invite
from ownvitals.auth.tables import oauth_flows
from ownvitals.db import system_session
from ownvitals.flags import effective as flags
from ownvitals.mail import send_logged

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

FIELD_MAX = 1024


class StartIn(BaseModel):
    purpose: Literal["sign_in", "link", "reauth"] = "sign_in"
    invite_code: str | None = Field(default=None, max_length=FIELD_MAX)
    return_to: str | None = Field(default=None, max_length=FIELD_MAX)
    locale: str | None = Field(default=None, max_length=FIELD_MAX)  # the page's (ADR 0015 §2)


class StartOut(BaseModel):
    authorization_url: str


class ProvidersOut(BaseModel):
    providers: list[str]


def _runtime(request: Request) -> OAuthRuntime:
    return request.app.state.oauth


def _redirect_uri(request: Request, provider: str) -> str:
    return f"{request.app.state.settings.public_base_url}/oauth/{provider}/callback"


@router.get("/api/auth/oauth/providers")
async def enabled(request: Request) -> ProvidersOut:
    return ProvidersOut(providers=sorted(_runtime(request).providers))


@router.post("/api/auth/oauth/{provider}/start")
async def start(
    provider: str,
    body: StartIn,
    request: Request,
    response: Response,
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> StartOut:
    prov = _runtime(request).providers.get(provider)
    if prov is None:
        raise HTTPException(status_code=404, detail={"reason": "provider_disabled"})
    await reserve(request, [Bucket(OAUTH_IP, bucket_ip(request_ip(request)))])
    user_id = None
    if body.purpose == "link":
        # Recent auth and a step-up before the start (section 6).
        ctx = await require_user(await current_context(request, info), info)
        await require_sensitive(ctx, info)
        user_id = ctx.user_id
    elif body.purpose == "reauth":
        ctx = await require_user(await current_context(request, info), info)
        user_id = ctx.user_id
        async with system_session(factory(request)) as db:
            if await has_password(db, user_id) is not False:  # a password, or the account gone
                raise HTTPException(status_code=403, detail={"reason": "reauth_use_password"})
    invite_id: UUID | None = None
    settings = request.app.state.settings
    if body.purpose == "sign_in" and body.invite_code:
        async with system_session(factory(request)) as db:
            invite_mode = await flags.signup_mode(db, settings.signup_mode) == "invite"  # or closed
            if invite_mode:
                invite_id = await valid_invite(db, body.invite_code)
        if invite_mode and invite_id is None:
            raise HTTPException(status_code=400, detail={"reason": "invalid_invite"})
    async with system_session(factory(request)) as db:
        flow = await create_flow(
            db,
            provider=provider,
            purpose=body.purpose,
            user_id=user_id,
            invite_id=invite_id,
            return_to=safe_return_to(body.return_to),
            locale=for_new_account(request, body.locale) if body.purpose == "sign_in" else None,
        )
    set_oauth_cookie(response, flow.cookie)
    return StartOut(
        authorization_url=await authorization_url(
            prov,
            _redirect_uri(request, provider),
            state=flow.state,
            nonce=flow.nonce,
            verifier=flow.verifier,
            reauth=body.purpose == "reauth",
        )
    )


def _target(flow: Flow | None, outcome: Outcome) -> str:
    purpose = flow.purpose if flow else "sign_in"
    if outcome.code != "ok":
        base = "/account" if purpose in ("link", "reauth") else "/signin"
        return f"{base}?oauth={outcome.code}"
    return_to = safe_return_to(flow.return_to if flow else None)  # checked again before use
    match outcome.next:
        case "signed_in":
            return return_to or "/"
        case "reauthed":
            return return_to or "/account?oauth=reauthed"
        case "linked":
            return "/account?oauth=linked"
        case _:
            return "/signin?oauth=email_needed"


async def _flow_age(db: AsyncSession, cookie: str) -> float | None:
    """Seconds since the flow's start by the database clock, read in the transaction that takes
    the flow (#247: a Google re-authentication must end within 5 minutes of its start)."""
    age = await db.execute(
        select(func.extract("epoch", func.now() - oauth_flows.c.created_at)).where(
            oauth_flows.c.flow_hash == token_hash(cookie)
        )
    )
    value = age.scalar_one_or_none()
    return None if value is None else float(value)


async def _handle(
    request: Request,
    provider: str,
    flow: Flow | None,
    info: SessionInfo | None,
    flow_age: float | None = None,
) -> Outcome:
    query = request.query_params
    state, code = query.get("state", ""), query.get("code")
    if flow is None:
        return Outcome("invalid", why="no_flow")  # no cookie, expired, or already used
    if flow.provider != provider:
        return Outcome("invalid", why="provider_mismatch")
    if not flow.state_matches(state):
        return Outcome("invalid", why="state_mismatch")
    if error := query.get("error"):
        return Outcome(
            "denied", why=f"provider_error={error if error in OAUTH_ERRORS else 'unrecognized'}"
        )
    if not code:
        return Outcome("denied", why="no_code")
    if flow.purpose in ("link", "reauth") and (info is None or info.user_id != flow.user_id):
        return Outcome("session_mismatch", why="no_session" if info is None else "other_user")
    runtime = _runtime(request)
    try:
        identity = await identify(
            runtime,
            runtime.providers[provider],
            _redirect_uri(request, provider),
            code=code,
            verifier=flow.verifier,
            nonce=flow.nonce,
        )
    except ProviderError as exc:
        # ``ProviderError`` names the failed check (fixed text), never a token or body.
        async with system_session(factory(request)) as db:
            await record_event(
                db,
                "oauth_failed",
                user_id=flow.user_id,
                ip=request_ip(request),
                detail={"provider": provider},
            )
        return Outcome("invalid", why=f"provider: {exc}")
    async with system_session(factory(request)) as db:
        return await apply(
            db,
            OutcomeRequest(
                settings=request.app.state.settings,
                keys=request.app.state.keys,
                flow=flow,
                identity=identity,
                info=info,
                session_token=request_session_token(request),
                device_token=request_device_token(request),
                pending_token=request.cookies.get(PENDING_COOKIE),
                ip=request_ip(request),
                user_agent=user_agent(request),
                flow_age=flow_age,
            ),
        )


@router.get("/oauth/{provider}/callback")
async def callback(
    provider: str,
    request: Request,
    background: BackgroundTasks,
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> Response:
    if provider not in _runtime(request).providers:
        raise HTTPException(status_code=404)
    await reserve(request, [Bucket(OAUTH_IP, bucket_ip(request_ip(request)))])
    cookie = request.cookies.get(OAUTH_COOKIE)
    flow, flow_age = None, None
    if cookie:
        async with system_session(factory(request)) as db:
            flow_age = await _flow_age(db, cookie)
            flow = await take_flow(db, cookie)  # before the code is exchanged: single use
    outcome = await _handle(request, provider, flow, info, flow_age)
    if outcome.code != "ok":
        logger.warning(
            "oauth callback refused: provider=%s purpose=%s outcome=%s why=%s",
            provider,
            flow.purpose if flow else "unknown",
            outcome.code,
            outcome.why or outcome.code,
        )
    response = RedirectResponse(_target(flow, outcome), status_code=303)
    clear_oauth_cookie(response)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    if outcome.issued is not None:
        set_session_cookie(response, outcome.issued)
    if outcome.device_token is not None:
        set_device_cookie(response, outcome.device_token)
    if outcome.new_pending_token is not None:
        set_pending_cookie(response, outcome.new_pending_token)
    if outcome.mail is not None:
        background.add_task(send_logged, mailer(request), outcome.mail)
    return response
