"""``/api/ai/digest``: the weekly digest's API (ADR 0014 sections 3 and 5; PLAN section 7.4; issue
209).

- ``GET /digest``: ``available`` (AI is enabled for the user: the flag and a live consent),
  ``enabled`` (the user's digest opt-in) and the newest digests, each with its week, status
  (``ready``, ``skipped_budget``, ``unavailable``) and, when ready, its text (the model's, shown
  as text only). Also while AI is off.
- ``PUT /digest`` with ``enabled``: opting in answers 409 ``ai_not_enabled`` unless AI is
  enabled for the user; opting out always works.

A full session for every route (``require_user``); the write goes through the auth
middleware's Origin and CSRF checks. Web never calls the model and never queues a digest.
Responses carry codes, dates and the user's own stored text, never a sentence of ours.
"""

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from ownvitals.ai import consent, digest_store
from ownvitals.auth.deps import require_user
from ownvitals.context import UserContext

router = APIRouter(prefix="/api/ai")


class DigestOut(BaseModel):
    week_start: date
    status: Literal["ready", "skipped_budget", "unavailable"]
    content: str | None
    created_at: datetime


class DigestViewOut(BaseModel):
    available: bool
    enabled: bool
    digests: list[DigestOut]


class OptIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool


class OptInOut(BaseModel):
    enabled: bool


@router.get("/digest", response_model=DigestViewOut)
async def read_digest(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> DigestViewOut:
    state = request.app.state
    view = await digest_store.read_view(state.session_factory, ctx, state.settings)
    return DigestViewOut(
        available=view.available,
        enabled=view.opted_in,
        digests=[
            DigestOut.model_validate(
                {
                    "week_start": d.week_start,
                    "status": d.status,
                    "content": d.content,
                    "created_at": d.created_at,
                }
            )
            for d in view.digests
        ],
    )


@router.put("/digest", response_model=OptInOut)
async def set_digest(
    body: OptIn, request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> OptInOut:
    state = request.app.state
    try:
        on = await digest_store.set_opt_in(state.session_factory, ctx, state.settings, body.enabled)
    except consent.AiNotEnabled:
        raise HTTPException(status_code=409, detail={"reason": "ai_not_enabled"}) from None
    return OptInOut(enabled=on)
