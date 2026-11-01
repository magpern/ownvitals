"""Google sign-in, linking and re-authentication (ADR 0003 sections 1 and 6).

``POST /api/auth/oauth/{provider}/start`` (CSRF-checked) creates the single-use flow and returns
the authorization URL. ``GET /oauth/{provider}/callback`` takes the flow atomically *before* the
code is exchanged, checks provider and state, and answers with a redirect: the outcome is a short
code in the query of an SPA page, never anything the provider sent. This route is registered
before the SPA catch-all; its query string (code, state) is never logged.
"""

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

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
    require_recent_auth,
    require_user,
)
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import factory, mailer, reserve, user_agent
from ownvitals.auth.oauth_client import OAuthRuntime, ProviderError, authorization_url, identify
from ownvitals.auth.oauth_outcomes import Outcome, apply
from ownvitals.auth.oauth_outcomes import Request as OutcomeRequest
from ownvitals.auth.oauth_store import Flow, create_flow, safe_return_to, take_flow
from ownvitals.auth.ratelimit import OAUTH_IP, Bucket
from ownvitals.auth.sessions import SessionInfo
from ownvitals.auth.signup_routes import valid_invite
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
        # Recent auth (and step-up with a confirmed TOTP) before the start (section 6).
        ctx = await require_user(await current_context(request, info))
        await require_recent_auth(ctx, info)
        user_id = ctx.user_id
    elif body.purpose == "reauth":
        ctx = await require_user(await current_context(request, info))
        user_id = ctx.user_id
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


async def _handle(
    request: Request, provider: str, flow: Flow | None, info: SessionInfo | None
) -> Outcome:
    query = request.query_params
    state, code = query.get("state", ""), query.get("code")
    if flow is None or flow.provider != provider or not flow.state_matches(state):
        return Outcome("invalid")
    if query.get("error") or not code:
        return Outcome("denied")
    if flow.purpose in ("link", "reauth") and (info is None or info.user_id != flow.user_id):
        return Outcome("session_mismatch")
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
        logger.warning("oauth callback refused (%s): %s", provider, exc)
        async with system_session(factory(request)) as db:
            await record_event(
                db,
                "oauth_failed",
                user_id=flow.user_id,
                ip=request_ip(request),
                detail={"provider": provider},
            )
        return Outcome("invalid")
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
    flow = None
    if cookie:
        async with system_session(factory(request)) as db:
            flow = await take_flow(db, cookie)  # before the code is exchanged: single use
    outcome = await _handle(request, provider, flow, info)
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
