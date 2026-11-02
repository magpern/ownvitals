"""The ``data_revisions`` counter (PLAN section 5).

A source insert, update or soft delete bumps the owner's stream in the same transaction.
An update that touches only ``ecg_recordings.estimated`` does not: that column is derived
output (ADR 0006 section 15), and bumping it would make every recompute look like new input.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_BUMP = text(
    "INSERT INTO data_revisions (owner_id, stream, revision) "
    "VALUES (:owner, :stream, 1) "
    "ON CONFLICT (owner_id, stream) DO UPDATE "
    "SET revision = data_revisions.revision + 1 RETURNING revision"
)


async def bump(db: AsyncSession, owner_id: UUID, stream: str) -> int:
    """Advance the counter and return the value this transaction stored."""
    revision = (await db.execute(_BUMP, {"owner": owner_id, "stream": stream})).scalar_one()
    return int(revision)


async def current(db: AsyncSession, owner_id: UUID, stream: str) -> int:
    row = (
        await db.execute(
            text(
                "SELECT revision FROM data_revisions WHERE owner_id = :owner AND stream = :stream"
            ),
            {"owner": owner_id, "stream": stream},
        )
    ).scalar_one_or_none()
    return 0 if row is None else int(row)
