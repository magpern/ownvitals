"""``/api/auth/qr``: QR approve-login (ADR 0011 sections 5 to 9).

The desktop (no session; the pre-session cookie of section 4 binds it):

- ``POST /start``: a new pre-session token and a request; answers the row id, the QR (an SVG
  rendered here, dark on light) and its URL, ``https://<host>/qr#<nonce>``. 20 per 10 minutes per
  IP (IPv6 by /64, the trusted-proxy client IP of ADR 0003 section 8).
- ``GET /{id}/events``: the stream of the request's state (``qrlogin_stream``); 404 for a row of
  another pre-session or none, 429 over the stream limits.
- ``POST /{id}/complete``: once approved, the new session's cookie, in this response.

The phone (a full session that was not itself born from a QR approval):

- ``POST /scan`` ``{nonce}``: the first scan wins; answers the desktop's browser, OS, IP and
  place, the time of the request and whether it is on another network. 10 per 10 minutes per
  account.
- ``POST /{id}/approve`` ``{code}``: a TOTP or recovery code entered for this approval; an earlier
  step-up does not count. 3 per hour per account, and the MFA buckets of ADR 0003 section 9.
- ``POST /{id}/deny``.

Every refusal of a code that is unknown, expired, used, another session's or another
pre-session's is the same 404 ``qr_unavailable`` ("this code has expired or was already used").
With ``qr_login_enabled`` false the start and scan routes answer 404 ``not_found``. Each step
writes an ``auth_events`` row; none holds a nonce, a cookie or a token (section 9).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ownvitals.auth import geo, mails, presession, qrlogin, totp
from ownvitals.auth.clientip import bucket_ip, same_network
from ownvitals.auth.cookies import clear_pre_cookie, set_pre_cookie, set_session_cookie
from ownvitals.auth.deps import current_session, request_ip, request_session_token, require_user
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import factory, mailer, reserve, user_agent
from ownvitals.auth.notices import notify_mfa_failures
from ownvitals.auth.qrlogin_stream import StreamSlots, qr_stream
from ownvitals.auth.ratelimit import (
    QR_APPROVE_ACCOUNT,
    QR_SCAN_ACCOUNT,
    QR_START_IP,
    Bucket,
    mfa_buckets,
)
from ownvitals.auth.sessions import SessionInfo
from ownvitals.auth.useragent import describe
from ownvitals.context import UserContext
from ownvitals.db import system_session
from ownvitals.jobs.hub import EventHub
from ownvitals.mail import send_logged

UNAVAILABLE = "qr_unavailable"
RETRY_AFTER_SECONDS = 5


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """One ``LISTEN qr_logins`` connection per web process, and the stream slots."""
    settings = app.state.settings
    hub = EventHub(app.state.engine, channel=qrlogin.CHANNEL)
    app.state.qr_hub = hub
    app.state.qr_streams = StreamSlots(settings.qr_streams_per_ip, settings.qr_streams_per_process)
    hub.start()
    try:
        yield
    finally:
        await hub.stop()


router = APIRouter(prefix="/api/auth/qr", tags=["auth"], lifespan=lifespan)

Info = Annotated[SessionInfo | None, Depends(current_session)]


class QrStarted(BaseModel):
    id: UUID
    qr_svg: str
    qr_url: str
    expires_in: int


class NonceIn(BaseModel):
    nonce: str = Field(min_length=1, max_length=128)


class CodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class DesktopOut(BaseModel):
    """The desktop as its browser reports it (section 7); ``place`` is "Place unknown" when
    there is no source."""

    browser: str
    os: str
    ip: str | None
    place: str


class ScanOut(BaseModel):
    id: UUID
    desktop: DesktopOut
    requested_at: datetime
    different_network: bool
    expires_in: int


class Done(BaseModel):
    status: str


class SignedIn(BaseModel):
    id: UUID
    email: str
    role: str


def _refuse(status: int, reason: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"reason": reason})


def _enabled(request: Request) -> None:
    if not request.app.state.settings.qr_login_enabled:
        _refuse(404, "not_found")


def _phone(info: SessionInfo | None) -> SessionInfo:
    """A full session that may approve: never one born from a QR approval (no chaining)."""
    if info is None:  # unreachable behind require_user
        _refuse(401, "not_authenticated")
    if not info.may_approve_qr:
        _refuse(403, "qr_approval_not_allowed")
    return info


async def _refused(
    request: Request, action: str, user_id: UUID | None, qr: UUID | None
) -> NoReturn:
    """Write ``qr_login_refused`` and answer the generic 404."""
    detail: dict[str, Any] = {"action": action}
    if qr is not None:
        detail["qr"] = str(qr)
    async with system_session(factory(request)) as db:
        await record_event(
            db, "qr_login_refused", user_id=user_id, ip=request_ip(request), detail=detail
        )
    _refuse(404, UNAVAILABLE)


# --- the desktop --------------------------------------------------------------------------------


@router.post("/start")
async def start(request: Request, response: Response) -> QrStarted:
    _enabled(request)
    settings, ip = request.app.state.settings, request_ip(request)
    await reserve(request, [Bucket(QR_START_IP, bucket_ip(ip))])
    browser, system = describe(user_agent(request))
    desktop = qrlogin.Desktop(ip, browser, system, geo.place(request, settings))
    async with system_session(factory(request)) as db:
        presession_id, token = await presession.issue(db, presession.request_pre_token(request))
        row_id, nonce = await qrlogin.start(db, presession_id, desktop)
        await record_event(db, "qr_login_started", ip=ip, detail={"qr": str(row_id)})
    set_pre_cookie(response, token)
    url = qrlogin.qr_url(settings, nonce)
    return QrStarted(
        id=row_id,
        qr_svg=totp.qr_svg(url),
        qr_url=url,
        expires_in=int(qrlogin.SCAN_WINDOW.total_seconds()),
    )


@router.get("/{qr_id}/events", response_model=None, responses={404: {}, 429: {}})
async def events(qr_id: UUID, request: Request) -> StreamingResponse | JSONResponse | Response:
    app = request.app
    pre_token = presession.request_pre_token(request)
    async with system_session(factory(request)) as db:
        presession_id = await presession.find(db, pre_token)
        bound = await qrlogin.is_bound(db, qr_id, presession_id)
        current = (
            await qrlogin.stream_state(db, qr_id, presession_id)
            if bound and presession_id is not None
            else None
        )
    if presession_id is None or current is None:
        _refuse(404, "not_found")  # another pre-session's row and an unknown one alike
    if current.state in qrlogin.ENDED and request.headers.get("last-event-id") == current.state:
        return Response(status_code=204)  # the browser has seen the end: stop reconnecting
    slots: StreamSlots = app.state.qr_streams
    slot = slots.acquire(qr_id, bucket_ip(request_ip(request)))
    if slot is None:
        return JSONResponse(
            {"detail": {"reason": "too_many_streams"}},
            status_code=429,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )

    async def frames() -> AsyncIterator[str]:
        try:
            async for chunk in qr_stream(factory(request), app.state.qr_hub, slot, presession_id):
                yield chunk
        finally:
            slots.release(slot)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/{qr_id}/complete")
async def complete(
    qr_id: UUID, request: Request, response: Response, background: BackgroundTasks
) -> SignedIn:
    settings, ip = request.app.state.settings, request_ip(request)
    pre_token = presession.request_pre_token(request)
    async with system_session(factory(request)) as db:
        presession_id = await presession.find(db, pre_token)
        done = await qrlogin.complete(
            db,
            settings,
            qr_id,
            presession_id,
            user_agent=user_agent(request),
            ip=ip,
            replacing_token=request_session_token(request),
        )
        if done is not None:
            await presession.end(db, pre_token)
    if done is None:
        await _refused(request, "complete", None, qr_id)
    set_session_cookie(response, done.issued)
    clear_pre_cookie(response)
    background.add_task(
        send_logged,
        mailer(request),
        mails.qr_login_approved_notice(
            settings,
            done.email,
            done.desktop.browser,
            done.desktop.os,
            geo.shown(done.desktop.place),
        ),
    )
    return SignedIn(id=done.user_id, email=done.email, role=done.role)


# --- the phone ----------------------------------------------------------------------------------


@router.post("/scan")
async def scan(
    body: NonceIn, request: Request, ctx: Annotated[UserContext, Depends(require_user)], info: Info
) -> ScanOut:
    _enabled(request)
    session = _phone(info)
    await reserve(request, [Bucket(QR_SCAN_ACCOUNT, str(ctx.user_id))])
    nonce = qrlogin.decode_nonce(body.nonce)
    ip = request_ip(request)
    scanned = None
    if nonce is not None:
        async with system_session(factory(request)) as db:
            scanned = await qrlogin.scan(db, nonce, session)
            if scanned is not None:
                await record_event(
                    db,
                    "qr_login_scanned",
                    user_id=ctx.user_id,
                    ip=ip,
                    detail={"qr": str(scanned.id), "place": scanned.desktop.place},
                )
    if scanned is None:
        await _refused(request, "scan", ctx.user_id, None)
    desktop = scanned.desktop
    return ScanOut(
        id=scanned.id,
        desktop=DesktopOut(
            browser=desktop.browser, os=desktop.os, ip=desktop.ip, place=geo.shown(desktop.place)
        ),
        requested_at=scanned.requested_at,
        different_network=not same_network(desktop.ip, ip),
        expires_in=max(int((scanned.approve_deadline - scanned.now).total_seconds()), 0),
    )


@router.post("/{qr_id}/approve", response_model=None)
async def approve(
    qr_id: UUID,
    body: CodeIn,
    request: Request,
    background: BackgroundTasks,
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Info,
) -> Done | JSONResponse:
    session = _phone(info)
    ip = request_ip(request)
    await reserve(request, [Bucket(QR_APPROVE_ACCOUNT, str(ctx.user_id))])
    attempt = await reserve(request, mfa_buckets(str(ctx.user_id), ip))
    async with system_session(factory(request)) as db:
        result = await qrlogin.approve(
            db,
            request.app.state.keys,
            request.app.state.keyring,
            qr_id,
            session,
            body.code,
            ip,
        )
    if result.outcome == "invalid_code":
        if result.mail_failures:
            background.add_task(notify_mfa_failures, ctx.user_id, session.email)
        return JSONResponse(
            {"detail": {"reason": "invalid_code"}}, status_code=400, background=background
        )
    await attempt.succeed()  # no code failed: the MFA slots go back
    if result.outcome == "unavailable":
        await _refused(request, "approve", ctx.user_id, qr_id)
    return Done(status="approved")


@router.post("/{qr_id}/deny")
async def deny(
    qr_id: UUID, request: Request, ctx: Annotated[UserContext, Depends(require_user)], info: Info
) -> Done:
    session = _phone(info)
    async with system_session(factory(request)) as db:
        denied = await qrlogin.deny(db, qr_id, session, request_ip(request))
    if not denied:
        await _refused(request, "deny", ctx.user_id, qr_id)
    return Done(status="denied")
