"""``/api/auth/qr``, the phone's routes: scan, approve, deny (ADR 0011 sections 5 to 9).

The routes, limits and refusals are described in ``qrlogin_routes``, whose helpers this module
uses; ``setup`` includes this router right after that one.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ownvitals.auth import geo, qrlogin, qrlogin_phone
from ownvitals.auth.clientip import same_network
from ownvitals.auth.deps import request_ip, require_user
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import factory, reserve
from ownvitals.auth.notices import notify_mfa_failures
from ownvitals.auth.qrlogin_routes import Info, _enabled, _refuse, _refused
from ownvitals.auth.ratelimit import QR_APPROVE_ACCOUNT, QR_SCAN_ACCOUNT, Bucket, mfa_buckets
from ownvitals.auth.sessions import SessionInfo
from ownvitals.context import UserContext
from ownvitals.db import system_session

router = APIRouter(prefix="/api/auth/qr", tags=["auth"])


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


def _phone(info: SessionInfo | None) -> SessionInfo:
    """A full session that may approve: never one born from a QR approval (no chaining)."""
    if info is None:  # unreachable behind require_user
        _refuse(401, "not_authenticated")
    if not info.may_approve_qr:
        _refuse(403, "qr_approval_not_allowed")
    return info


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
            scanned = await qrlogin_phone.scan(db, nonce, session)
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
        result = await qrlogin_phone.approve(
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
        denied = await qrlogin_phone.deny(db, qr_id, session, request_ip(request))
    if not denied:
        await _refused(request, "deny", ctx.user_id, qr_id)
    return Done(status="denied")
