"""``GET /api/scores``: the signed-in user's latest score of each kind (ADR 0012 §5, PLAN §8).

One ``user_session`` (forced RLS; every statement also filters ``owner_id``). A kind with no row
is absent; the page shows "not computed yet" then. ``status`` is one of ``computed``,
``insufficient_data``, ``not_available``, ``not_supported``, ``not_set``; ``value`` is set only
when computed; ``stale`` is true when the data changed since the row was written. ``his`` is in
the experimental tier (D9): it is listed only while the owner-only switch is on, whatever is
stored. The response carries text keys, never words: the page holds the text, and none of it
names a quality.
"""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ownvitals.analysis.scores.his_table import REGISTERED
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
    stale: bool
    algo_version: str
    how_key: str | None
    components: dict[str, Any]
    # Provenance for the "How it's computed" panel (#493). The revision is a 63-bit digest, so it
    # is sent as text (a JSON number would lose digits in a browser).
    input_revision: str
    input_fingerprint: str
    table_sha: str | None


class ScoresOut(BaseModel):
    scores: list[ScoreOut]


def _table_sha(kind: str, components: dict[str, Any]) -> str | None:
    """The registered SHA-256 of the band table a ``his`` row used (its id is stored)."""
    table = components.get("table")
    if kind != "his" or not isinstance(table, dict):
        return None
    return REGISTERED.get(str(table.get("id")))


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
                stale=stale,
                algo_version=r.algo_version,
                how_key=r.components.get("text_key"),
                components=dict(r.components),
                input_revision=str(r.input_revision),
                input_fingerprint=bytes(r.input_fingerprint).hex(),
                table_sha=_table_sha(r.kind, r.components),
            )
            for r, stale in rows
        ]
    )
