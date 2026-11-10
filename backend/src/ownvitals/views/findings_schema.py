"""The finding as the read API returns it (``FindingOut``, with its gate ``GateOut``): shared by
``/api/findings``, the area summaries (``views.schemas``, ``views.nerve_routes``) and anything
that imports the models without the routes, so a worker-side import never loads the web's
auth dependencies (moved out of ``views.findings_routes`` unchanged, #208).
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from ownvitals.views.findings_read import FindingView


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
