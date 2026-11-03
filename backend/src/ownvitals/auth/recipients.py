"""Where outbox mail goes (ADR 0007 section 2; #194): the account tables are ``ownvitals.auth``'s
(guard 8), so the outbox sender asks here, at the time it sends. No address is ever stored in the
outbox.

- A user's mail goes to the account's verified email (ADR 0003 section 1: every account's email
  is verified), and to no one when the account is gone or disabled.
- An owner notice goes to the owner, that is the admins (ADR 0004 section 6): every admin account
  that is not disabled.

The addresses are returned to the caller and never logged.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.tables import users
from ownvitals.db import system_session

Factory = async_sessionmaker[AsyncSession]


async def user_address(factory: Factory, user_id: UUID) -> str | None:
    """The verified email of an active account, or ``None``."""
    async with system_session(factory) as db:
        return (
            await db.execute(
                select(users.c.email).where(
                    users.c.id == user_id,
                    users.c.disabled_at.is_(None),
                    users.c.email_verified_at.is_not(None),
                )
            )
        ).scalar_one_or_none()


async def admin_addresses(factory: Factory) -> list[str]:
    """The verified emails of the active admin accounts, in a stable order."""
    async with system_session(factory) as db:
        rows = await db.execute(
            select(users.c.email)
            .where(
                users.c.role == "admin",
                users.c.disabled_at.is_(None),
                users.c.email_verified_at.is_not(None),
            )
            .order_by(users.c.created_at, users.c.id)
        )
        return [str(email) for email in rows.scalars()]
