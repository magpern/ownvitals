"""``/api/reports/doctor``: request, list and download the doctor PDF (ADR 0013 sections 4 and
6; issue 210).

The same job, chunk and download code as ``/api/exports`` (``export_store`` and
``export_routes.serve_download``), for rows of kind ``doctor_pdf``:

- ``POST`` behind ``require_stepup`` (a step-up within 10 minutes; the data export asks for
  recent authentication too, this one does not). The body is ``DoctorChoices``: a period of at
  most 12 months, metric keys, at most 10 ECG recording ids; no name, no email, no free text.
  202 with the new export; 409 ``export_in_progress``; 429 ``busy``; 429 ``rate_limited`` above
  10 requests in 24 hours.
- ``GET``: the user's latest doctor reports (full session).
- ``GET /{id}/file``: the PDF, once: step-up, ``Sec-Fetch-Site: same-origin``, RLS (404 for
  another user's id), 3 attempts, 24 hours.

Audit: ``doctor_pdf_requested`` and ``doctor_pdf_downloaded`` (ids and counts only).
"""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from ownvitals.auth.deps import (
    current_session,
    request_ip,
    require_stepup,
    require_user,
    reserve_doctor_request,
)
from ownvitals.auth.exports import Requested, record_event
from ownvitals.context import UserContext
from ownvitals.reports import export_store
from ownvitals.reports.doctor import KIND
from ownvitals.reports.doctor_input import DoctorChoices
from ownvitals.reports.export_routes import (
    ExportList,
    ExportOne,
    _factory,
    _out,
    _refuse,
    _tidied,
    serve_download,
)
from ownvitals.reports.export_store import Refused

router = APIRouter(prefix="/api/reports/doctor", tags=["reports"])
DOCTOR = "doctor_pdf"


@router.post("", status_code=202, response_model=ExportOne)
async def request_doctor_pdf(
    body: DoctorChoices,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_stepup)],
) -> ExportOne:
    ip = request_ip(request)
    factory = _factory(request)

    async def reserve() -> None:
        await reserve_doctor_request(request, ctx)

    try:
        done = await export_store.request(
            factory,
            ctx,
            job_limit=request.app.state.settings.jobs_per_user,
            reserve=reserve,
            kind=DOCTOR,
            job_kind=KIND,
            job_input=body.model_dump(mode="json"),
        )
    except Refused as refused:
        await _tidied(request, ctx, refused.tidied, ip)
        raise _refuse(refused.reason) from None
    await _tidied(request, ctx, done.tidied, ip)
    detail = Requested(export_id=done.export_id, export_kind=DOCTOR)
    await record_event(factory, ctx, "doctor_pdf_requested", detail, ip=ip)
    views, _ = await export_store.list_exports(factory, ctx, DOCTOR)
    return ExportOne(export=_out(next(v for v in views if v.id == done.export_id)))


@router.get("", response_model=ExportList)
async def list_doctor_pdfs(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> ExportList:
    views, tidied = await export_store.list_exports(_factory(request), ctx, DOCTOR)
    await _tidied(request, ctx, tidied, request_ip(request))
    return ExportList(exports=[_out(v) for v in views])


@router.get("/{export_id}/file", response_model=None)
async def download_doctor_pdf(
    export_id: UUID,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
    info: Annotated[Any, Depends(current_session)],
) -> StreamingResponse | HTMLResponse:
    day = datetime.now(UTC).date().isoformat()
    return await serve_download(
        export_id,
        request,
        ctx,
        info,
        kind=DOCTOR,
        media_type="application/pdf",
        filename=f"ownvitals-health-report-{day}.pdf",
        audit="doctor_pdf_downloaded",
    )
