"""``GET /api/profile`` and ``PUT /api/profile`` (issue #203, PLAN section 8 "Settings"): the
analysis profile (birth year, sex, day assignment), display, overview layout and notification
choices of the signed-in user, on ``user_profiles`` (#181).

The caller is ``require_user`` (a full session); only the caller's own row is read or written,
under forced RLS, and a change needs the Origin and CSRF checks like every write. The body is
validated in ``profile_models``. Changing the sex or the day assignment queues the daily pass
for the user's connection (it re-derives and then evaluates: #186, #187); the queue is
best-effort, as after the normaliser, and a user with no connection has nothing to recompute.
"""

import logging
from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from ownvitals.auth.deps import require_user
from ownvitals.connections.repository import list_connections
from ownvitals.context import UserContext
from ownvitals.derived.daily_job import enqueue_daily
from ownvitals.views.deps import Now
from ownvitals.views.profile_models import ProfileIn, ProfileOut, moves_the_analysis
from ownvitals.views.profile_store import read_profile, write_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


async def _recompute(request: Request, ctx: UserContext) -> None:
    factory = request.app.state.session_factory
    live = [c for c in await list_connections(factory, ctx) if c.status != "disconnected"]
    if live:
        await enqueue_daily(factory, live[0].id, "live")


@router.get("/profile", response_model=ProfileOut)
async def get_profile(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> ProfileOut:
    return await read_profile(request.app.state.session_factory, ctx, request.app.state.settings)


@router.put("/profile", response_model=ProfileOut)
async def put_profile(
    body: ProfileIn,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
    now: Now,
) -> ProfileOut:
    if body.birth_year is not None and body.birth_year > now.astimezone(UTC).year:
        raise HTTPException(status_code=400, detail={"reason": "birth_year_in_the_future"})
    written = await write_profile(
        request.app.state.session_factory, ctx, request.app.state.settings, body
    )
    if written is None:
        raise HTTPException(status_code=400, detail={"reason": "experimental_not_available"})
    old, stored = written
    if moves_the_analysis(old, body):
        await _recompute(request, ctx)
    return stored
