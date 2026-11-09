"""``/api/ecg``: the signed-in user's ECG recordings (ADR 0006 section 3, PLAN 7.3 step 2).

``GET /api/ecg`` lists them (time with zone, device, Withings' classification as Withings', its
heart rate and the linked 135 to 138 values); ``GET /api/ecg/{id}`` adds the stored signal as
int32 microvolts per lead, the sampling rate and the lead label ("lead unverified", A2).
Withings' values are labelled as theirs and a missing one is ``null``, which the page words
"not reported", never 0 or normal. Our estimates come only through the one read function
(``views.findings_read``), only behind the owner-only switch, in their own ``ownvitals_estimate``
object that the page shows under ``ecg.estimate.label``; without the switch it is ``null``.
``GET /api/ecg/{id}.pdf`` is the true-scale PDF: Withings' values only, never an estimate.

Another user's recording answers 404, like a missing one. Nothing here logs a sample or a value.
Responses are ``no-store`` (the auth middleware sets it on every ``/api`` answer).
"""

import asyncio
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from ownvitals.auth.deps import require_user
from ownvitals.context import UserContext
from ownvitals.ecg import display
from ownvitals.reports.ecg_facts import facts_for
from ownvitals.reports.ecg_pdf import TooLong, build_pdf
from ownvitals.views.ecg_read import (
    LIST_DEFAULT,
    LIST_MAX,
    Recording,
    list_recordings,
    read_recording,
)
from ownvitals.views.findings_read import read_ecg_estimates

router = APIRouter(prefix="/api/ecg")


class DeviceOut(BaseModel):
    model_id: int | None
    name: str | None  # a model Withings documents; otherwise null and the page shows the id


class ClassificationOut(BaseModel):
    """Withings' own classification, attributed (PLAN 7.2). ``meaning`` is null for a code
    Withings does not document (the page says "Withings code n") and for no code."""

    source: Literal["withings"] = "withings"
    code: int | None
    meaning: str | None
    details_code: int | None
    details_meaning: str | None


class IntervalOut(BaseModel):
    source: Literal["withings"] = "withings"
    key: str
    value: float
    unit: Literal["ms"] = "ms"


class RecordingOut(BaseModel):
    id: UUID
    recorded_at: datetime
    source_tz: str
    recorded_local: str
    device: DeviceOut
    classification: ClassificationOut
    heart_rate_bpm: int | None  # "Reported by Withings"; null reads "not reported"
    intervals: list[IntervalOut]  # the linked 135 to 138 values
    has_signal: bool

    @classmethod
    def of(cls, r: Recording) -> "RecordingOut":
        return cls(
            id=r.id,
            recorded_at=r.recorded_at,
            source_tz=r.source_tz,
            recorded_local=_local_iso(r.recorded_at, r.source_tz),
            device=DeviceOut(model_id=r.model_id, name=display.model_name(r.model_id)),
            classification=ClassificationOut(
                code=r.afib,
                meaning=display.afib_meaning(r.afib),
                details_code=r.afib_details,
                details_meaning=display.details_meaning(r.afib_details),
            ),
            heart_rate_bpm=r.heart_rate,
            intervals=[IntervalOut(key=i.key, value=i.value) for i in r.intervals],
            has_signal=r.has_signal,
        )


def _local_iso(at: datetime, zone: str) -> str:
    try:
        return at.astimezone(ZoneInfo(zone)).isoformat()
    except (ZoneInfoNotFoundError, ValueError):
        return at.isoformat()


class RecordingsOut(BaseModel):
    recordings: list[RecordingOut]
    next_offset: int | None


class SignalOut(BaseModel):
    """The stored signal: int32 microvolts per lead, unfiltered and unscaled."""

    sampling_hz: int
    unit: Literal["uV"] = "uV"
    lead_label: str  # "lead unverified" (A2), per lead
    leads: list[list[int]]
    lead_labels: list[str]


class EstimateOut(BaseModel):
    """Our estimate: experimental, only behind the owner-only switch (section 9). The page shows
    it in its own panel under ``label_key``, never in a Withings slot."""

    source: Literal["ownvitals_estimate"] = "ownvitals_estimate"
    label_key: Literal["ecg.estimate.label"] = "ecg.estimate.label"
    gate: str | None
    reasons: list[str]
    hr_bpm: float | None
    hrv: dict[str, Any] | None
    rpeaks: list[float] | None
    windows: list[dict[str, Any]]


class RecordingDetailOut(BaseModel):
    recording: RecordingOut
    wear_position: int | None
    signal: SignalOut | None  # null when no signal is stored or it does not decode
    ownvitals_estimate: EstimateOut | None


async def _own(request: Request, ctx: UserContext, recording_id: UUID) -> Recording:
    found = await read_recording(request.app.state.session_factory, ctx, recording_id)
    if found is None:
        raise HTTPException(status_code=404)
    return found


@router.get("", response_model=RecordingsOut)
async def list_ecg(
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
    limit: Annotated[int, Query(ge=1, le=LIST_MAX)] = LIST_DEFAULT,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> RecordingsOut:
    found, more = await list_recordings(
        request.app.state.session_factory, ctx, limit=limit, offset=offset
    )
    return RecordingsOut(
        recordings=[RecordingOut.of(r) for r in found],
        next_offset=offset + limit if more else None,
    )


@router.get("/{recording_id}.pdf")
async def ecg_pdf(
    recording_id: UUID, request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> Response:
    """Withings' values and the stored signal only; the estimates are never read here."""
    r = await _own(request, ctx, recording_id)
    if r.leads is None or r.sampling_hz is None:
        raise HTTPException(status_code=404)
    try:
        # about 1.2 s of CPU: off the event loop, same bytes (the module is pure)
        body = await asyncio.to_thread(build_pdf, facts_for(r, r.leads, r.sampling_hz))
    except TooLong:
        raise HTTPException(status_code=422) from None
    return Response(
        body,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="ecg-{r.id}.pdf"'},
    )


@router.get("/{recording_id}", response_model=RecordingDetailOut)
async def get_ecg(
    recording_id: UUID, request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> RecordingDetailOut:
    r = await _own(request, ctx, recording_id)
    signal = None
    if r.leads is not None and r.sampling_hz is not None:
        signal = SignalOut(
            sampling_hz=r.sampling_hz,
            lead_label=display.LEAD_UNVERIFIED,
            leads=[list(lead) for lead in r.leads],
            lead_labels=[display.lead_label(i, len(r.leads)) for i in range(len(r.leads))],
        )
    shown = await read_ecg_estimates(
        request.app.state.session_factory, ctx, request.app.state.settings, [r.id]
    )
    view = shown.get(r.id)
    estimate = (
        None
        if view is None
        else EstimateOut(
            gate=view.gate,
            reasons=list(view.reasons),
            hr_bpm=view.hr_bpm,
            hrv=None if view.hrv is None else dict(view.hrv),
            rpeaks=None if view.rpeaks is None else list(view.rpeaks),
            windows=[dict(w) for w in view.windows],
        )
    )
    return RecordingDetailOut(
        recording=RecordingOut.of(r),
        wear_position=r.wearposition,
        signal=signal,
        ownvitals_estimate=estimate,
    )
