"""Account helpers for the admin Users view (#127, guard 8).

``ownvitals_maint`` has no grant on the account tables, so these functions open their own
``system_session`` on the app role. The admin area calls the names in
``PUBLIC_ENTRY_POINTS`` and never sees a table. Nothing here reads ``password_hash``, a
session token, an IP or a user agent.

- ``list_accounts`` / ``find_account``: a page, or one account, with the second factor, the
  live session count and the latest ``sign_in`` (never a measurement).
- ``account_emails``: ``{id: email}`` for ids the caller already holds.
- ``disable_account`` / ``enable_account``: ``disabled_at``, and disable also deletes every
  session. Neither touches an admin (the role stays with the operator CLI), so the admin area
  cannot undo an operator's disable of an admin either (#277).
- ``delete_account``: the same ``delete_user_account`` the user's own
  ``DELETE /api/auth/account`` runs. Refuses an admin and an address that is not the account's.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.events import record_event
from ownvitals.auth.sessions import count_live_sessions, revoke_user_sessions
from ownvitals.auth.tables import auth_events, mfa_totp, users
from ownvitals.config import Settings
from ownvitals.db import system_session
from ownvitals.emailnorm import normalize_email

Factory = async_sessionmaker[AsyncSession]
MAX_IDS = 100
RoleName = Literal["user", "admin"]
SecondFactor = Literal["on", "setup_pending"]


class AccountMissing(Exception):
    """No account has this id."""


class TargetIsAdmin(Exception):
    """Disable, enable and delete refuse an admin, including the acting admin."""


class EmailMismatch(Exception):
    """The typed address is not this account's. The address is not kept on the exception."""


@dataclass(frozen=True, slots=True, repr=False)
class AccountSummary:
    """One account as the Users view may show it. ``repr`` omits the address."""

    id: UUID
    email: str
    email_normalized: str
    role: RoleName
    disabled_at: datetime | None
    created_at: datetime
    second_factor: SecondFactor
    last_sign_in: datetime | None
    sessions: int

    def __repr__(self) -> str:
        return f"{type(self).__name__}(id={self.id})"


def _role(value: str) -> RoleName:
    if value == "admin":
        return "admin"
    if value == "user":
        return "user"
    raise RuntimeError("account role is not user or admin")


async def delete_user_account(db: AsyncSession, user_id: UUID, *, ip: str | None) -> None:
    """The account-delete path: the ``account_deleted`` event, then the ``users`` row.

    The user's own route and ``delete_account`` both call this in the caller's transaction.
    The event has no ``user_id`` (the row is about to go; the foreign key would null it).
    Rows that reference the user cascade or null, as the schema says.
    """
    await record_event(db, "account_deleted", ip=ip)
    await db.execute(delete(users).where(users.c.id == user_id))


async def _locked(db: AsyncSession, user_id: UUID) -> tuple[str, str] | None:
    """``(role, email_normalized)`` with the row locked, or ``None``. The same lock and mode as
    ``userlock.lock_user_row`` (first, ``FOR NO KEY UPDATE``), which this cannot call: it reads
    ``password_hash``. A sign-in that waits for it finds the account disabled or gone."""
    row = (
        await db.execute(
            select(users.c.role, users.c.email_normalized)
            .where(users.c.id == user_id)
            .with_for_update(key_share=True)
        )
    ).one_or_none()
    return None if row is None else (row.role, row.email_normalized)


async def _extras(
    db: AsyncSession, settings: Settings, ids: list[UUID]
) -> tuple[set[UUID], dict[UUID, datetime], dict[UUID, int]]:
    """Confirmed authenticators, the latest ``sign_in``, and live session counts."""
    if not ids:
        return set(), {}, {}
    totp = {
        row.user_id
        for row in (
            await db.execute(
                select(mfa_totp.c.user_id).where(
                    mfa_totp.c.user_id.in_(ids), mfa_totp.c.confirmed_at.is_not(None)
                )
            )
        ).all()
    }
    last_at = func.max(auth_events.c.at).label("last_at")
    signed_in = {
        row.user_id: row.last_at
        for row in (
            await db.execute(
                select(auth_events.c.user_id, last_at)
                .where(auth_events.c.user_id.in_(ids), auth_events.c.kind == "sign_in")
                .group_by(auth_events.c.user_id)
            )
        ).all()
    }
    counts = await count_live_sessions(db, settings, ids)
    return totp, signed_in, counts


def _summary(
    row: Any,
    *,
    totp: set[UUID],
    signed_in: dict[UUID, datetime],
    counts: dict[UUID, int],
) -> AccountSummary:
    user_id: UUID = row.id
    return AccountSummary(
        id=user_id,
        email=row.email,
        email_normalized=row.email_normalized,
        role=_role(row.role),
        disabled_at=row.disabled_at,
        created_at=row.created_at,
        second_factor="on" if user_id in totp else "setup_pending",
        last_sign_in=signed_in.get(user_id),
        sessions=counts.get(user_id, 0),
    )


_COLUMNS = (
    users.c.id,
    users.c.email,
    users.c.email_normalized,
    users.c.role,
    users.c.disabled_at,
    users.c.created_at,
)


async def list_accounts(
    factory: Factory,
    settings: Settings,
    *,
    prefix: str | None,
    offset: int,
    limit: int,
) -> list[AccountSummary]:
    """Accounts in address order, optionally those whose normalized address starts with
    ``prefix``. ``limit`` is at most 101 (a page of 100 plus one, so the caller can see
    whether another page exists)."""
    if limit < 1 or limit > MAX_IDS + 1 or offset < 0:
        raise ValueError("account page is out of range")
    async with system_session(factory) as db:
        statement = select(*_COLUMNS)
        if prefix:
            statement = statement.where(
                users.c.email_normalized.startswith(normalize_email(prefix), autoescape=True)
            )
        rows = (
            await db.execute(
                statement.order_by(users.c.email_normalized, users.c.id).offset(offset).limit(limit)
            )
        ).all()
        totp, signed_in, counts = await _extras(db, settings, [row.id for row in rows])
    return [_summary(row, totp=totp, signed_in=signed_in, counts=counts) for row in rows]


async def find_account(
    factory: Factory, settings: Settings, user_id: UUID
) -> AccountSummary | None:
    """One account, or ``None``."""
    async with system_session(factory) as db:
        row = (await db.execute(select(*_COLUMNS).where(users.c.id == user_id))).one_or_none()
        if row is None:
            return None
        totp, signed_in, counts = await _extras(db, settings, [row.id])
    return _summary(row, totp=totp, signed_in=signed_in, counts=counts)


async def account_emails(factory: Factory, ids: Collection[UUID]) -> dict[UUID, str]:
    """``{user id: email}`` for those of ``ids`` that are accounts (at most ``MAX_IDS``)."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    if len(wanted) > MAX_IDS:
        raise ValueError(f"at most {MAX_IDS} accounts per lookup")
    async with system_session(factory) as db:
        rows = await db.execute(select(users.c.id, users.c.email).where(users.c.id.in_(wanted)))
    return {row.id: row.email for row in rows.all()}


async def disable_account(factory: Factory, user_id: UUID) -> tuple[datetime, int]:
    """Set ``disabled_at`` (an earlier value is kept) and delete every session.

    Returns ``(disabled_at, sessions deleted)``. Raises ``AccountMissing`` or
    ``TargetIsAdmin`` and changes nothing in those cases.
    """
    async with system_session(factory) as db:
        locked = await _locked(db, user_id)
        if locked is None:
            raise AccountMissing()
        if locked[0] == "admin":
            raise TargetIsAdmin()
        disabled_at = (
            await db.execute(
                update(users)
                .where(users.c.id == user_id)
                .values(disabled_at=func.coalesce(users.c.disabled_at, func.now()))
                .returning(users.c.disabled_at)
            )
        ).scalar_one()
        revoked = await revoke_user_sessions(db, user_id)
    return disabled_at, revoked


async def enable_account(factory: Factory, user_id: UUID) -> None:
    """Clear ``disabled_at``. Raises ``AccountMissing`` or ``TargetIsAdmin`` and changes nothing
    in those cases."""
    async with system_session(factory) as db:
        locked = await _locked(db, user_id)
        if locked is None:
            raise AccountMissing()
        if locked[0] == "admin":
            raise TargetIsAdmin()
        await db.execute(update(users).where(users.c.id == user_id).values(disabled_at=None))


async def delete_account(factory: Factory, user_id: UUID, *, email: str, ip: str | None) -> None:
    """Delete through ``delete_user_account``. Refuses an admin and a mismatched address,
    and then nothing is deleted."""
    async with system_session(factory) as db:
        locked = await _locked(db, user_id)
        if locked is None:
            raise AccountMissing()
        role, normalized = locked
        if role == "admin":
            raise TargetIsAdmin()
        if normalized != normalize_email(email):
            raise EmailMismatch()
        await delete_user_account(db, user_id, ip=ip)
