"""``/api/findings``: the signed-in user's findings (PLAN §8, the Findings mock-up; ADR 0006 section
11; ADR 0010).

Both routes read only through ``views.findings_read`` (the one read function) as the
``dashboard`` consumer: a hidden finding is not listed and its id answers 404, like a missing
one, another user's, or an experimental one without the owner-only switch. The response carries
text keys for "why you're seeing this" and "what it isn't" (``finding.<rule>.why`` and
``finding.<rule>.isnt``); the page holds the words. Responses are ``no-store`` (the auth
middleware sets it on every ``/api`` answer).
"""

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ownvitals.auth.deps import require_user
from ownvitals.context import UserContext
from ownvitals.views.findings_read import (
    FindingsQuery,
    FindingView,
    Tab,
    read_finding,
    read_findings,
)

router = APIRouter(prefix="/api/findings", tags=["findings"])


class GateOut(BaseModel):
    outcome: str
    counted: int
    failed: int


class FindingOut(BaseModel):
    id: UUID
    rule_id: str
    rule_version: str
    tier: str
    area: str | None
    severity: str
    confidence: str
    origin: str
    status: str
    subject: str
    metric_keys: list[str]
    window_start: date
    window_end: date
    reference_day: date | None
    first_seen: datetime
    last_seen: datetime
    ended_at: datetime | None
    seen_at: datetime | None
    data_quality: dict[str, Any]
    not_evaluated: bool  # section 11's "not evaluated by ownvitals" on E4 and E6
    gate: GateOut | None  # only behind the owner-only switch
    why_key: str
    isnt_key: str

    @classmethod
    def of(cls, view: FindingView) -> "FindingOut":
        return cls(
            id=view.id,
            rule_id=view.rule_id,
            rule_version=view.rule_version,
            tier=view.tier,
            area=view.area,
            severity=view.severity,
            confidence=view.confidence,
            origin=view.origin,
            status=view.status,
            subject=view.subject,
            metric_keys=list(view.metric_keys),
            window_start=view.window_start,
            window_end=view.window_end,
            reference_day=view.reference_day,
            first_seen=view.first_seen,
            last_seen=view.last_seen,
            ended_at=view.ended_at,
            seen_at=view.seen_at,
            data_quality=dict(view.data_quality),
            not_evaluated=view.not_evaluated,
            gate=(
                None
                if view.gate is None
                else GateOut(
                    outcome=view.gate.outcome, counted=view.gate.counted, failed=view.gate.failed
                )
            ),
            why_key=f"finding.{view.rule_id}.why",
            isnt_key=f"finding.{view.rule_id}.isnt",
        )


class FindingsOut(BaseModel):
    findings: list[FindingOut]


class ReadingOut(BaseModel):
    metric_key: str
    ts: datetime
    value: float
    grpid: int | None
    quality_flags: list[str]


class EstimateOut(BaseModel):
    """Our estimate of one linked recording: experimental, only behind the switch (section 9).
    Labelled "Estimated by ownvitals from the raw signal; not a Withings measurement"."""

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
) -> FindingsOut:
    result = await read_findings(
        request.app.state.session_factory,
        ctx,
        request.app.state.settings,
        consumer="dashboard",
        query=FindingsQuery(tab=tab, tier=tier, area=area),
    )
    return FindingsOut(findings=[FindingOut.of(view) for view in result.findings])


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
