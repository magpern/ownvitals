"""The read of the latest score per kind (#188), used by ``views.reads.Reader`` inside its
``user_session`` (forced RLS; ``owner_id`` filtered too). Moved out of ``views.reads`` unchanged.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.config import Settings
from ownvitals.derived.scores_revision import revision_digest
from ownvitals.flags import effective

_LATEST_SCORES = text(
    "SELECT DISTINCT ON (kind) kind, day, value, components, algo_version, input_revision "
    "FROM scores "
    "WHERE owner_id = :o ORDER BY kind, day DESC"
)
_SCORE_REVISION = text(
    "SELECT stream, revision FROM data_revisions WHERE owner_id = :o "
    "AND stream IN ('measures', 'sleep_summary', 'activity_daily')"
)


async def latest_scores(
    db: AsyncSession, owner: UUID, settings: Settings
) -> list[tuple[Any, bool]]:
    """The latest stored row of each score kind with its stale state (#188): stale when the
    digest of the stream revisions is no longer the row's ``input_revision`` (data changed or
    was purged since; ADR 0012 §1, the pass has not caught up). ``his`` (experimental tier) only
    while the owner-only switch is on, whatever is stored."""
    switch = await effective.experimental_findings(db, settings)
    rows = (await db.execute(_LATEST_SCORES, {"o": owner})).all()
    found = {r.stream: int(r.revision) for r in await db.execute(_SCORE_REVISION, {"o": owner})}
    current = revision_digest(
        (
            found.get("measures", 0),
            found.get("sleep_summary", 0),
            found.get("activity_daily", 0),
        )
    )
    return [(r, int(r.input_revision) != current) for r in rows if switch or r.kind != "his"]
