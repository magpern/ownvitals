"""Account emails for the admin area's Usage and Jobs views (ADR 0011 section 6, AD3 #221).

Guard 8 keeps every statement on the account tables in this package, so ``ownvitals.admin.usage``
and ``ownvitals.admin.jobs`` call ``account_emails`` with the session they hold: the audited
``admin_session`` on ``ownvitals_maint``, whose column grants allow reading ``users.id`` and
``users.email``. Nothing here opens a session or reads anything else of an account.
"""

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.tables import users

MAX_IDS = 1000


async def account_emails(db: AsyncSession, ids: Collection[UUID]) -> dict[UUID, str]:
    """``{user id: email}`` for those of ``ids`` that are accounts (at most ``MAX_IDS``)."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    if len(wanted) > MAX_IDS:
        raise ValueError(f"at most {MAX_IDS} accounts per lookup")
    rows = await db.execute(select(users.c.id, users.c.email).where(users.c.id.in_(wanted)))
    return {row.id: row.email for row in rows.all()}
