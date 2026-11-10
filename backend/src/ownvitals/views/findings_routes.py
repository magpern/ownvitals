"""``/api/findings``: the signed-in user's findings (PLAN §8, the Findings mock-up; ADR 0006 section
11; ADR 0010).

Both routes read only through ``views.findings_read`` (the one read function) as the
``dashboard`` consumer: a hidden finding is not listed and its id answers 404, like a missing
one, another user's, or an experimental one without the owner-only switch. The list pages with
``limit`` (default 50, at most 200) and ``offset``; ``next_offset`` is null on the last page,
and a page can hold fewer than ``limit`` findings when some are hidden. The response carries text
keys for "why you're seeing this" and "what it isn't" (``finding.<rule>.why`` and
``finding.<rule>.isnt``); the page holds the words. Responses are ``no-store`` (the auth
middleware sets it on every ``/api`` answer).
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ownvitals.auth.deps import require_user
from ownvitals.context import UserContext
from ownvitals.views.findings_read import (
    PAGE_DEFAULT,
    PAGE_MAX,
    FindingsQuery,
    Tab,
    read_finding,
    read_findings,
)
from ownvitals.views.findings_schema import FindingOut

router = APIRouter(prefix="/api/findings")


class FindingsOut(BaseModel):
    findings: list[FindingOut]
    next_offset: int | None  # pass as ``offset`` for the next page; null on the last page


class ReadingOut(BaseModel):
    metric_key: str
    ts: datetime
    value: float
    grpid: int | None
    quality_flags: list[str]


class EstimateOut(BaseModel):
    """Our estimate of one linked recording: experimental, only behind the switch (section 9).
    The page shows it in its own panel under ``label_key`` ("Estimated by ownvitals from the raw
    signal; not a Withings measurement"), never in a Withings slot."""

    source: Literal["ownvitals_estimate"] = "ownvitals_estimate"
    label_key: Literal["ecg.estimate.label"] = "ecg.estimate.label"
    recording_id: UUID
    grpid: int
    gate: str | None
    reasons: list[str]
    hr_bpm: float | None
    hrv: dict[str, Any] | None


class FindingDetailOut(BaseModel):
    finding: FindingOut
    evidence: dict[str, Any]
    readings: list[ReadingOut]
    estimates: list[EstimateOut]


Tier = Annotated[str | None, Query(pattern="^(device|trend|experimental|system)$")]
Area = Annotated[str | None, Query(pattern="^(body|heart|nerve|activity|system)$")]


@router.get("", response_model=FindingsOut)
async def list_findings(
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
    tab: Tab = "live",
    tier: Tier = None,
    area: Area = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> FindingsOut:
    result = await read_findings(
        request.app.state.session_factory,
        ctx,
        request.app.state.settings,
        consumer="dashboard",
        query=FindingsQuery(tab=tab, tier=tier, area=area, limit=limit, offset=offset),
    )
    return FindingsOut(
        findings=[FindingOut.of(view) for view in result.findings],
        next_offset=result.next_offset,
    )


@router.get("/{finding_id}", response_model=FindingDetailOut)
async def get_finding(
    finding_id: UUID,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
) -> FindingDetailOut:
    detail = await read_finding(
        request.app.state.session_factory,
        ctx,
        request.app.state.settings,
        finding_id,
        consumer="dashboard",
    )
    if detail is None:
        raise HTTPException(status_code=404)
    return FindingDetailOut(
        finding=FindingOut.of(detail.finding),
        evidence=dict(detail.finding.evidence),
        readings=[
            ReadingOut(
                metric_key=r.metric_key,
                ts=r.ts,
                value=float(r.value),
                grpid=r.grpid,
                quality_flags=list(r.quality_flags),
            )
            for r in detail.readings
        ],
        estimates=[
            EstimateOut(
                recording_id=e.recording_id,
                grpid=e.grpid,
                gate=e.view.gate,
                reasons=list(e.view.reasons),
                hr_bpm=e.view.hr_bpm,
                hrv=None if e.view.hrv is None else dict(e.view.hrv),
            )
            for e in detail.estimates
        ],
    )
