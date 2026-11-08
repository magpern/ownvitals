"""``GET /api/auth/activity``: the signed-in user's own security events, newest first (#251).

What a row shows is decided in ``activity_model`` (an allowlist of kinds and of fields). Here:

- **Only the caller's rows.** The query filters on the ``user_id`` of the request's
  ``UserContext`` (``require_user``, a full session) and takes no parameter that names a user;
  the cursor only bounds that same filter. ``auth_events`` is a system table without RLS (ADR
  0002 section 3), so this filter is the wall, and the tests prove it with two users.
- **Paged and bounded.** ``limit`` is 1 to ``PAGE_MAX`` (default ``PAGE_DEFAULT``), ``before``
  is the ``next_before`` of the previous page; a cursor that is not one we could make is a 422,
  echoing nothing. Rows are purged after 30 days, so no page reaches further back.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.activity_model import (
    CURSOR_MAX,
    PAGE_DEFAULT,
    PAGE_MAX,
    STORED_KINDS,
    ActivityEntry,
    StoredEvent,
    decode_cursor,
    encode_cursor,
    entry,
)
from ownvitals.auth.deps import require_user
from ownvitals.auth.tables import auth_events
from ownvitals.context import UserContext
from ownvitals.db import system_session

router = APIRouter(prefix="/api/auth", tags=["auth"])


class ActivityItemOut(BaseModel):
    kind: str  # a code (``activity_model.SHOWN_KINDS``), worded by the frontend's catalogue
    at: datetime
    method: str | None  # password, google, passkey or qr; null when the kind has none
    browser: str | None  # a ``useragent.BROWSERS`` family; null when not recorded
    os: str | None  # a ``useragent.SYSTEMS`` family; null when not recorded
    place: str  # "City, CC", "CC" or "Place unknown"


class ActivityOut(BaseModel):
    events: list[ActivityItemOut]
    next_before: str | None  # pass as ``before`` for the next, older page; null at the end


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


async def read_activity(
    db: AsyncSession, user_id: UUID, *, limit: int, before: tuple[datetime, UUID] | None
) -> tuple[list[ActivityEntry], str | None]:
    """The user's own shown events, newest first, at most ``limit``, older than ``before``.
    Ties in time are ordered by id, so paging never skips or repeats a row."""
    if not 1 <= limit <= PAGE_MAX:
        raise ValueError("limit out of range")
    statement = (
        select(
            auth_events.c.id,
            auth_events.c.kind,
            auth_events.c.at,
            auth_events.c.detail,
            auth_events.c.browser,
            auth_events.c.os,
            auth_events.c.place,
        )
        .where(auth_events.c.user_id == user_id, auth_events.c.kind.in_(STORED_KINDS))
        .order_by(auth_events.c.at.desc(), auth_events.c.id.desc())
        .limit(limit + 1)
    )
    if before is not None:
        statement = statement.where(tuple_(auth_events.c.at, auth_events.c.id) < tuple_(*before))
    rows = [StoredEvent(*row) for row in (await db.execute(statement)).all()]
    page = rows[:limit]
    entries = [shown for row in page if (shown := entry(row)) is not None]
    more = len(rows) > limit
    return entries, encode_cursor(page[-1].at, page[-1].id) if more else None


@router.get("/activity")
async def list_own_activity(
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
    before: Annotated[str | None, Query(max_length=CURSOR_MAX)] = None,
) -> ActivityOut:
    position = None if before is None else decode_cursor(before)
    if before is not None and position is None:
        raise HTTPException(status_code=422, detail={"reason": "invalid_cursor"})
    async with system_session(_factory(request)) as db:
        entries, cursor = await read_activity(db, ctx.user_id, limit=limit, before=position)
    return ActivityOut(
        events=[
            ActivityItemOut(
                kind=item.kind,
                at=item.at,
                method=item.method,
                browser=item.browser,
                os=item.os,
                place=item.place,
            )
            for item in entries
        ],
        next_before=cursor,
    )
