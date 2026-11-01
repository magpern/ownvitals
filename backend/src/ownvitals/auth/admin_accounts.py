"""The account side of the admin area's Users screen (ADR 0011 section 6, AD1 #218).

Guard 8 keeps every statement on the account tables in this package, so ``ownvitals.admin.users``
calls these with the session it holds: its audited ``admin_session`` on ``ownvitals_maint``,
whose column grants (``docker/postgres/maint/30-maint.sql``) allow exactly what is used here.
Nothing here opens a session, and nothing reads ``password_hash`` or a session's token, IP or
agent.

- ``find_accounts``: a page of accounts, optionally by email prefix (on ``email_normalized``),
  ordered by that address and id.
- ``get_account``: one account, or ``None``; with ``lock=True`` ``FOR UPDATE``, so the role a
  disable or delete checks cannot change (``admin grant`` from the CLI) before it commits.
- ``disable_account``: sets ``disabled_at`` (an earlier value is kept) and deletes every session
  of the user (ADR 0003 section 3), so their next request is 401.
- ``enable_account``: clears ``disabled_at``.
- ``delete_account``: ADR 0003's account deletion (#37): the ``users`` row goes and the foreign
  keys cascade; the user's own path writes the same statement.

None of these changes ``users.role``: granting and revoking admin stay with the CLI (ADR 0003
section 7).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.sessions import revoke_user_sessions
from ownvitals.auth.tables import users
from ownvitals.emailnorm import normalize_email


@dataclass(frozen=True, slots=True)
class AccountRow:
    id: UUID
    email: str
    email_normalized: str
    role: str
    disabled_at: datetime | None
    created_at: datetime


_COLUMNS = (
    users.c.id,
    users.c.email,
    users.c.email_normalized,
    users.c.role,
    users.c.disabled_at,
    users.c.created_at,
)


def _row(row: Any) -> AccountRow:
    return AccountRow(
        row.id, row.email, row.email_normalized, row.role, row.disabled_at, row.created_at
    )


async def find_accounts(
    db: AsyncSession, *, prefix: str | None, offset: int, limit: int
) -> list[AccountRow]:
    """Accounts whose normalized address starts with ``prefix`` (all with ``None``), in order."""
    statement = select(*_COLUMNS)
    if prefix:
        statement = statement.where(
            users.c.email_normalized.startswith(normalize_email(prefix), autoescape=True)
        )
    statement = statement.order_by(users.c.email_normalized, users.c.id).offset(offset).limit(limit)
    return [_row(row) for row in (await db.execute(statement)).all()]


async def get_account(db: AsyncSession, user_id: UUID, *, lock: bool = False) -> AccountRow | None:
    statement = select(*_COLUMNS).where(users.c.id == user_id)
    if lock:
        statement = statement.with_for_update()
    row = (await db.execute(statement)).one_or_none()
    return None if row is None else _row(row)


async def disable_account(db: AsyncSession, user_id: UUID) -> tuple[datetime, int]:
    """``(disabled_at, sessions deleted)``."""
    disabled_at = (
        await db.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(disabled_at=func.coalesce(users.c.disabled_at, func.now()))
            .returning(users.c.disabled_at)
        )
    ).scalar_one()
    return disabled_at, await revoke_user_sessions(db, user_id)


async def enable_account(db: AsyncSession, user_id: UUID) -> None:
    await db.execute(update(users).where(users.c.id == user_id).values(disabled_at=None))


async def delete_account(db: AsyncSession, user_id: UUID) -> bool:
    """Delete the account; its rows cascade. ``False`` when there was none."""
    result = await db.execute(delete(users).where(users.c.id == user_id).returning(users.c.id))
    return result.first() is not None
