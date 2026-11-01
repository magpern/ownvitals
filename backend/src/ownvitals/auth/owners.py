"""Account checks for the job issuer (ADR 0005 section 4): ``ownvitals.jobs`` asks here, because
only ``ownvitals.auth`` touches the account tables (guard 8). The worker loads this module, so it
imports nothing web-only (``tests/egress_worker.py``).

``ACCOUNTS`` is the worker's account directory (``ownvitals.jobs.issuer.Accounts``): an owner is
active when the account exists and is not disabled. Its connection refs are empty until the
connections layer (#13) reads them.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.tables import users
from ownvitals.context import UserContext
from ownvitals.db import system_session

Factory = async_sessionmaker[AsyncSession]


async def owner_active(db: AsyncSession, user_id: UUID) -> bool:
    """Whether the user exists and is not disabled. ``db`` is a ``system_session``."""
    row = (
        await db.execute(
            select(users.c.id).where(users.c.id == user_id, users.c.disabled_at.is_(None))
        )
    ).first()
    return row is not None


class AuthAccounts:
    """The account directory the worker's job issuer asks (``Accounts``)."""

    async def owner_active(self, factory: Factory, owner_id: UUID) -> bool:
        async with system_session(factory) as db:
            return await owner_active(db, owner_id)

    async def connection_refs(
        self, factory: Factory, ctx: UserContext
    ) -> frozenset[tuple[str, UUID]]:
        """None yet: ``ownvitals.connections`` reads them from #13."""
        return frozenset()


ACCOUNTS = AuthAccounts()
