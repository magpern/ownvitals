"""``GET /api/status``: the freshness line on every screen (PLAN section 8, the design's
principle 4): the last reading, the last sync, the imports running or incomplete per stream,
broken connections and the capability states. Read as the signed-in user under forced RLS.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ownvitals.auth.deps import require_user
from ownvitals.context import UserContext
from ownvitals.views import states
from ownvitals.views.catalog import STATUS_STREAMS
from ownvitals.views.deps import Now, settle
from ownvitals.views.reads import reading
from ownvitals.views.schemas import StatusOut, StreamStatusOut

router = APIRouter(prefix="/api/status")


@router.get("", response_model=StatusOut)
async def get_status(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)], now: Now
) -> StatusOut:
    async with reading(request.app.state.session_factory, ctx) as r:
        facts = await r.status_facts(now)
    connection = facts.connection
    if connection is None:
        link = states.NOT_CONNECTED
    elif connection.status == "needs_reconnect":
        link = states.NEEDS_RECONNECT
    elif states.plan_blocked(connection, None):
        link = states.PLUS_NEEDED
    else:
        link = "connected"
    streams = []
    for name in STATUS_STREAMS:
        if name not in facts.streams:
            continue
        cursor = facts.streams[name]
        settled = settle(facts, name, now)
        streams.append(
            StreamStatusOut(
                stream=name,
                state=settled.state,
                capability=cursor.capability,
                progress_percent=settled.progress_percent,
                last_ok_at=cursor.last_ok_at,
                skipped_out_of_range=cursor.skipped_out_of_range,
            )
        )
    return StatusOut(
        as_of=now,
        connection=link,
        last_reading_at=facts.last_reading_at,
        last_sync_at=facts.last_sync_at,
        streams=streams,
        importing=[s.stream for s in streams if s.state == states.IMPORTING],
        incomplete=[s.stream for s in streams if s.state == states.INCOMPLETE],
        broken_connections=(
            ["withings"] if link in {states.NEEDS_RECONNECT, states.PLUS_NEEDED} else []
        ),
    )
