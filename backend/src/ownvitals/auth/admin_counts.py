"""Account counts for the admin Overview (issue 549, guard 8).

``ownvitals_maint`` has no grant on the account tables, so, like ``admin_accounts``, this reads
them in its own ``system_session`` on the app role. Two numbers only: how many accounts exist,
and how many have a confirmed authenticator (``mfa_totp`` has one row per account), which is the
Users view's ``second_factor == "on"``. No address, id, role or time leaves it.
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.tables import mfa_totp, users
from ownvitals.db import system_session


@dataclass(frozen=True, slots=True)
class AccountCounts:
    accounts: int
    second_factor_on: int


async def account_counts(factory: async_sessionmaker[AsyncSession]) -> AccountCounts:
    """How many accounts there are, and how many of them have a confirmed authenticator."""
    confirmed = (
        select(func.count())
        .select_from(mfa_totp)
        .where(mfa_totp.c.confirmed_at.is_not(None))
        .scalar_subquery()
    )
    statement = select(func.count().label("n"), confirmed.label("on")).select_from(users)
    async with system_session(factory) as db:
        row = (await db.execute(statement)).one()
    return AccountCounts(accounts=int(row.n), second_factor_on=int(row.on))
