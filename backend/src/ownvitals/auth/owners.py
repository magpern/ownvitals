"""Account checks for the job issuer (ADR 0005 section 3): ``ownvitals.jobs`` asks here, because
only ``ownvitals.auth`` touches the account tables (guard 8)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.tables import users


async def owner_active(db: AsyncSession, user_id: UUID) -> bool:
    """Whether the user exists and is not disabled. ``db`` is a ``system_session``."""
    row = (
        await db.execute(
            select(users.c.id).where(users.c.id == user_id, users.c.disabled_at.is_(None))
        )
    ).first()
    return row is not None
