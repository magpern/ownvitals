"""``/api/exports``: request, list and download the JSON/CSV export (ADR 0013 section 4; #211).

- ``POST /api/exports`` behind ``require_sensitive`` (recent authentication **and** a step-up,
  both within 10 minutes; ADR 0003 section 4's export row). 202 with the new export; 409
  ``export_in_progress`` while one builds; 429 ``busy`` when the user's job slots are full; 429
  ``rate_limited`` with ``Retry-After`` above 3 requests in 24 hours.
- ``GET /api/exports``: the user's latest exports (full session). No content, hash or job.
- ``GET /api/exports/{id}/file``: the archive, authorised by the session cookie and a step-up
  within 10 minutes at download time, under RLS (404 for another user's id or an unknown one).
  **No URL secret**: the path holds only the id. The request must carry ``Sec-Fetch-Site:
  same-origin``; anything else, or none, gets 403 ``download_same_origin_required`` as a small
  page that links to Settings, without counting an attempt. Then 410 ``export_expired``, 409
  ``export_not_ready``, 410 ``export_download_spent`` (3 attempts used). The body streams from
  the chunks; only the latest attempt reads them, and the last chunk is taken in the transaction
  that marks the export ``expired`` and deletes its chunks, so of parallel attempts at most one
  receives the whole archive.

Audit (section 5, ``ownvitals.auth.exports.record_event``): ``export_requested``,
``export_downloaded`` (attempt number, whether it completed) and ``export_expired``; the build
writes ``export_ready`` and ``export_failed``. Never content, never the URL. Logs name no export
id.
"""

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.deps import (
    current_session,
    request_ip,
    require_sensitive,
    require_stepup,
    require_user,
    reserve_export_request,
)
from ownvitals.auth.exports import (
    Downloaded,
    Expired,
    ExportEventKind,
    Failed,
    Requested,
    record_event,
)
from ownvitals.context import UserContext
from ownvitals.reports import export_store
from ownvitals.reports.export_store import ExportView, Refused, Tidied

router = APIRouter(prefix="/api/exports", tags=["exports"])
_LOG = logging.getLogger(__name__)

# Each refusal of the store, as its status and body.
REFUSALS: Final[dict[str, tuple[int, dict[str, str]]]] = {
    "not_found": (404, {"reason": "not_found"}),
    "export_in_progress": (409, {"reason": "export_in_progress"}),
    "export_not_ready": (409, {"reason": "export_not_ready"}),
    "export_expired": (410, {"reason": "export_expired"}),
    "export_download_spent": (410, {"reason": "export_download_spent"}),
    "busy": (429, {"reason": "busy"}),
}
SAME_ORIGIN_PAGE: Final = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    "<title>ownvitals</title></head><body><p>Start the download from your ownvitals Settings "
    'page.</p><p><a href="/settings/data">Open Settings</a></p></body></html>'
)


class ExportOut(BaseModel):
    id: UUID
    state: str  # building, ready, failed or expired
    created_at: datetime
    ready_at: datetime | None
    expires_at: datetime | None
    size: int | None
    failure: str | None  # export_too_large or build_failed
    largest: str | None  # with export_too_large: the file that was largest
    attempts_left: int


class ExportOne(BaseModel):
    export: ExportOut


class ExportList(BaseModel):
    exports: list[ExportOut]


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


def _out(view: ExportView) -> ExportOut:
    return ExportOut(**{name: getattr(view, name) for name in ExportOut.model_fields})


def _refuse(reason: str) -> HTTPException:
    status, detail = REFUSALS[reason]
    return HTTPException(status_code=status, detail=dict(detail))


async def _tidied(request: Request, ctx: UserContext, tidied: Tidied, ip: str | None) -> None:
    """The audit of what the user's tidy changed: builds that ended without an archive, then
    archives that expired."""
    factory = _factory(request)
    for export_id in tidied.failed:
        detail = Failed(export_id=export_id, reason="build_failed")
        await record_event(factory, ctx, "export_failed", detail, ip=ip)
    for export_id in tidied.expired:
        await record_event(factory, ctx, "export_expired", Expired(export_id=export_id), ip=ip)


@router.post("", status_code=202, response_model=ExportOne)
async def request_export(
    request: Request, ctx: Annotated[UserContext, Depends(require_sensitive)]
) -> ExportOne:
    ip = request_ip(request)
    factory = _factory(request)

    async def reserve() -> None:
        await reserve_export_request(request, ctx)

    try:
        done = await export_store.request(
            factory, ctx, job_limit=request.app.state.settings.jobs_per_user, reserve=reserve
        )
    except Refused as refused:
        await _tidied(request, ctx, refused.tidied, ip)
        raise _refuse(refused.reason) from None
    await _tidied(request, ctx, done.tidied, ip)
    await record_event(factory, ctx, "export_requested", Requested(export_id=done.export_id), ip=ip)
    views, _ = await export_store.list_exports(factory, ctx)
    view = next(v for v in views if v.id == done.export_id)
    return ExportOne(export=_out(view))


@router.get("", response_model=ExportList)
async def list_exports(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> ExportList:
    views, tidied = await export_store.list_exports(_factory(request), ctx)
    await _tidied(request, ctx, tidied, request_ip(request))
    return ExportList(exports=[_out(v) for v in views])


def _filename() -> str:
    return f"ownvitals-export-{datetime.now(UTC).date().isoformat()}.zip"


async def serve_download(
    export_id: UUID,
    request: Request,
    ctx: UserContext,
    info: Any,
    *,
    kind: str,
    media_type: str,
    filename: str,
    audit: ExportEventKind,
) -> StreamingResponse | HTMLResponse:
    """The single-use, step-up-guarded download of one export of ``kind`` (ADR 0013 section 4),
    shared with the doctor PDF. ``audit`` is the event kind recorded for each attempt."""
    # Cross-site first, before the step-up and before any read: nothing is counted.
    if request.headers.get("sec-fetch-site") != "same-origin":
        return HTMLResponse(
            SAME_ORIGIN_PAGE,
            status_code=403,
            headers={
                "Cache-Control": "no-store",
                "X-Ownvitals-Reason": "download_same_origin_required",
            },
        )
    await require_stepup(ctx, info)
    factory = _factory(request)
    ip = request_ip(request)
    try:
        download = await export_store.begin_download(factory, ctx, export_id, kind)
    except Refused as refused:
        await _tidied(request, ctx, refused.tidied, ip)
        raise _refuse(refused.reason) from None

    async def body() -> AsyncIterator[bytes]:
        completed = False
        attempt, last = download.attempt, download.chunks - 1
        try:
            if last < 0:
                raise LookupError("no chunk")
            for seq in range(last):
                yield await export_store.read_chunk(factory, ctx, export_id, seq, attempt)
            # The last chunk and the single use commit together (parallel attempts: one wins).
            final = await export_store.take_last_chunk(factory, ctx, export_id, last, attempt)
            completed = True
            yield final
        except LookupError:
            _LOG.warning("export download ended early: a chunk is gone or a later attempt began")
        finally:
            # Best effort when the client went away mid-stream (the attempt is counted already).
            detail = Downloaded(
                export_id=export_id,
                attempt=download.attempt,
                completed=completed,
                export_kind="doctor_pdf" if kind == "doctor_pdf" else "data",
            )
            await record_event(factory, ctx, audit, detail, ip=ip)

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Length": str(download.size),
    }
    return StreamingResponse(body(), media_type=media_type, headers=headers)


@router.get("/{export_id}/file", response_model=None)
async def download_export(
    export_id: UUID,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Annotated[Any, Depends(current_session)],
) -> StreamingResponse | HTMLResponse:
    return await serve_download(
        export_id,
        request,
        ctx,
        info,
        kind="data",
        media_type="application/zip",
        filename=_filename(),
        audit="export_downloaded",
    )
