"""``/api/notifications``: the signed-in user's in-app notifications for the account menu (#195;
PLAN §7.1 step 7, the Settings mock-up's "Live findings and notices about your data always
appear under Findings").

A notification is a live finding whose notification was sent (``notify.findings``). The list
reads only through ``views.findings_read`` (the one read function) as the ``notification``
consumer: the experimental tier only with the owner-only switch, and a finding the overlay now
hides is left out (it is not shown anywhere; the mail that went out is not recalled).
``not_evaluated`` is section 11's "not evaluated by ownvitals" mark on E4 and E6, at this read.
Of the ``PAGE_MAX`` sent findings seen most recently, newest first by when it was sent, at most
``limit`` (default 20, at most 50); ``unseen`` counts those of the list the user has not opened.
No value, evidence or finding text: the menu links to the finding by id. Responses are
``no-store`` (the auth middleware sets it on every ``/api`` answer).
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from ownvitals.auth.deps import require_user
from ownvitals.context import UserContext
from ownvitals.views.findings_read import PAGE_MAX, FindingsQuery, read_findings

router = APIRouter(prefix="/api/notifications")

LIMIT_DEFAULT = 20
LIMIT_MAX = 50


class NotificationOut(BaseModel):
    finding_id: UUID
    rule_id: str
    tier: str
    area: str | None
    severity: str
    notified_at: datetime
    seen: bool
    not_evaluated: bool


class NotificationsOut(BaseModel):
    notifications: list[NotificationOut]
    unseen: int


@router.get("", response_model=NotificationsOut)
async def list_notifications(
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
    limit: Annotated[int, Query(ge=1, le=LIMIT_MAX)] = LIMIT_DEFAULT,
) -> NotificationsOut:
    result = await read_findings(
        request.app.state.session_factory,
        ctx,
        request.app.state.settings,
        consumer="notification",
        query=FindingsQuery(tab="live", notified=True, limit=PAGE_MAX),
    )
    sent = [view for view in result.findings if view.notified_at is not None]
    sent.sort(key=lambda view: (view.notified_at, view.id), reverse=True)
    items = [
        NotificationOut(
            finding_id=view.id,
            rule_id=view.rule_id,
            tier=view.tier,
            area=view.area,
            severity=view.severity,
            notified_at=view.notified_at,
            seen=view.seen_at is not None,
            not_evaluated=view.not_evaluated,
        )
        for view in sent[:limit]
        if view.notified_at is not None
    ]
    return NotificationsOut(notifications=items, unseen=sum(1 for item in items if not item.seen))
