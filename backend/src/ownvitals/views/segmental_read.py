"""The read of the latest segmental reading per body position (issue #324), used by
``views.reads.Reader`` inside its ``user_session`` (forced RLS; ``owner_id`` filtered too).
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.analysis.quality import EXCLUDING
from ownvitals.views.read_records import SegmentalRecord

_SEGMENTAL = text(
    "SELECT DISTINCT ON (metric_key) metric_key, CAST(ts AT TIME ZONE source_tz AS date) AS day, "
    "value FROM observations WHERE owner_id = :o AND starts_with(metric_key, :prefix) "
    "AND NOT (quality_flags && CAST(:excluding AS text[])) ORDER BY metric_key, ts DESC, id"
)
_EXCLUDING = sorted(flag.value for flag in EXCLUDING)


async def latest_segmental(db: AsyncSession, owner: UUID, key: str) -> list[SegmentalRecord]:
    """The newest usable reading of each body position of a segmental metric (``key`` is the
    part before the colon): segmental values have no daily value (ADR 0010), so the tile is
    the latest reading itself. Not bounded by ``ts``: the newest row per position can be
    anywhere in the partitioned table, and a bound would only hide an old reading."""
    rows = (
        await db.execute(_SEGMENTAL, {"o": owner, "prefix": f"{key}:", "excluding": _EXCLUDING})
    ).all()
    return [SegmentalRecord(r.metric_key, r.day, r.value) for r in rows]
