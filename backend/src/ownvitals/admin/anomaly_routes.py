"""The admin abuse view (#461, ADR 0018): recent session anomalies across users.

``GET /api/admin/anomalies?limit=&before_at=&before_id=``: the ``session_anomaly`` events of the
last 30 days, newest first, paged by the cursor ``(before_at, before_id)`` of the last row of the
previous page (both or neither; the answer's ``next`` is that pair, ``null`` on the last page).

**Read only, behind the gate and a step-up.** Every route here has ``admin_gate``; this read
also needs a step-up within the admin window (``stepped_up``, five minutes), because it shows
other people's accounts: without one it answers 403 ``step_up_required``, which the admin
area's handler audits as an ``admin.request`` refusal. Past both, it runs in an
``admin_session`` (``anomaly.list``, with the page size and whether it is the first page, never
the cursor) on the maint role, read only after its audit row, and reads through the definer
``admin_session_anomalies`` of ``docker/postgres/maint/70-admin-accounts.sql``, which refuses
unless that transaction holds this admin's ``ok`` row. ``ownvitals_maint`` holds no grant on
``auth_events`` or ``users``.

**What a row shows**: the event's id and time, the account's id and address (``null`` once the
account is gone), the signal codes (only those of ``SIGNALS``) and the coarse origin (browser and
system family, place). Never the event's address, a session, a token or the ``User-Agent``.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import text

from ownvitals.admin.db import AdminRefused, admin_session
from ownvitals.admin.routes import (
    STEPUP_REASON,
    AdminActor,
    admin_router,
    require_db,
    stepped_up,
)
from ownvitals.auth.deps import current_session
from ownvitals.auth.session_anomaly_policy import SIGNALS

router = admin_router()

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
_LIST = text(
    "SELECT id, at, user_id, email, signals, browser, os, place "
    "FROM public.admin_session_anomalies(:n, :before_at, :before_id)"
)


class AnomalyOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    at: datetime
    user_id: UUID | None  # None once the account is deleted
    email: str | None
    signals: list[str]
    browser: str | None
    os: str | None
    place: str | None


class Cursor(BaseModel):
    model_config = ConfigDict(frozen=True)

    before_at: datetime
    before_id: UUID


class AnomalyPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    rows: list[AnomalyOut]
    next: Cursor | None


def _signals(value: Any) -> list[str]:
    """The stored codes that are signals, in ``SIGNALS`` order; anything else is dropped."""
    found = set(value) if isinstance(value, list) else set()  # pyright: ignore[reportUnknownArgumentType]
    return [code for code in SIGNALS if code in found]


@router.get("/anomalies", response_model=AnomalyPage)
async def list_anomalies(
    request: Request,
    actor: AdminActor,
    info: Annotated[Any, Depends(current_session)],
    before_at: Annotated[AwareDatetime | None, Query()] = None,
    before_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> AnomalyPage:
    if info is None or info.user_id != actor.user_id:
        raise AdminRefused("forbidden", 403)  # the gate resolved another session: never
    if not stepped_up(info):
        raise AdminRefused(STEPUP_REASON, 403)  # audited by the AdminRefused handler
    if (before_at is None) != (before_id is None):
        raise AdminRefused("cursor_invalid", 422)
    page: Literal["first", "next"] = "first" if before_at is None else "next"
    params = {"limit": limit, "page": page}
    db = require_db(request)
    async with admin_session(db, actor, "anomaly.list", params=params) as session:
        found = (
            await session.execute(
                _LIST, {"n": limit + 1, "before_at": before_at, "before_id": before_id}
            )
        ).all()
    rows = [
        AnomalyOut(
            id=r.id,
            at=r.at,
            user_id=r.user_id,
            email=r.email,
            signals=_signals(r.signals),
            browser=r.browser,
            os=r.os,
            place=r.place,
        )
        for r in found[:limit]
    ]
    last = rows[-1] if len(found) > limit else None
    return AnomalyPage(
        rows=rows, next=Cursor(before_at=last.at, before_id=last.id) if last else None
    )
