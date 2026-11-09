"""``GET /api/scores``: the signed-in user's latest score of each kind (ADR 0012 §5, PLAN §8).

One ``user_session`` (forced RLS; every statement also filters ``owner_id``). A kind with no row
is absent; the page shows "not computed yet" then. ``status`` is one of ``computed``,
``insufficient_data``, ``not_available``, ``not_supported``, ``not_set``; ``value`` is set only
when computed. ``his`` is in the experimental tier (D9): it is listed only while the owner-only
switch is on, whatever is stored. The response carries text keys, never words: the page holds
the text, and none of it names a quality.
"""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ownvitals.auth.deps import require_user
from ownvitals.context import UserContext
from ownvitals.views.reads import reading

router = APIRouter(prefix="/api/scores")


class ScoreOut(BaseModel):
    kind: str
    day: date
    status: str
    value: float | None
    tier: str
    algo_version: str
    how_key: str | None
    components: dict[str, Any]


class ScoresOut(BaseModel):
    scores: list[ScoreOut]


def _tier(kind: str) -> str:
    return "experimental" if kind == "his" else "trend"


@router.get("", response_model=ScoresOut)
async def list_scores(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> ScoresOut:
    async with reading(request.app.state.session_factory, ctx) as reader:
        rows = await reader.latest_scores(settings=request.app.state.settings)
    return ScoresOut(
        scores=[
            ScoreOut(
                kind=r.kind,
                day=r.day,
                status=str(r.components["status"]),
                value=None if r.value is None else float(r.value),
                tier=_tier(r.kind),
                algo_version=r.algo_version,
                how_key=r.components.get("text_key"),
                components=dict(r.components),
            )
            for r in rows
        ]
    )
