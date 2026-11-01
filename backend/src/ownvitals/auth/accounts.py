"""Users, invites and admin roles (ADR 0003 sections 1 and 7).

These are the functions behind ``python -m ownvitals invite create`` and ``admin ...``, and the
ones sign-in (#37 to #39) calls. All take the caller's ``AsyncSession`` (a ``system_session``),
except the admin area's invite functions (AD2 #220), which take its ``admin_session``.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.devices import forget_devices
from ownvitals.auth.events import record_event
from ownvitals.auth.sessions import revoke_user_sessions
from ownvitals.auth.tables import invites, mfa_recovery_codes, mfa_totp, users
from ownvitals.config import Settings
from ownvitals.emailnorm import normalize_email


class AccountError(Exception):
    """A refusal the CLI shows to the operator (never contains secrets)."""


@dataclass(frozen=True, slots=True)
class UserRow:
    id: UUID
    email: str
    email_normalized: str
    role: str
    disabled: bool


async def create_user(
    db: AsyncSession, email: str, *, password_hash: str | None = None, role: str = "user"
) -> UUID:
    """Create a user whose email is already proven (ADR 0003 section 1: no unverified state)."""
    return (
        await db.execute(
            insert(users)
            .values(
                email=email.strip(),
                email_normalized=normalize_email(email),
                email_verified_at=func.now(),
                password_hash=password_hash,
                role=role,
            )
            .returning(users.c.id)
        )
    ).scalar_one()


async def find_user(db: AsyncSession, email: str) -> UserRow | None:
    row = (
        await db.execute(
            select(
                users.c.id,
                users.c.email,
                users.c.email_normalized,
                users.c.role,
                users.c.disabled_at,
            ).where(users.c.email_normalized == normalize_email(email))
        )
    ).first()
    if row is None:
        return None
    return UserRow(row.id, row.email, row.email_normalized, row.role, row.disabled_at is not None)


async def _require_user(db: AsyncSession, email: str) -> UserRow:
    user = await find_user(db, email)
    if user is None:
        raise AccountError("no user has that email address")
    return user


def _new_invite_code() -> tuple[str, bytes]:
    """An invite code (16 random bytes) to show once, and the SHA-256 that is stored."""
    code = secrets.token_urlsafe(16)
    return code, hashlib.sha256(code.encode()).digest()


async def create_invite(db: AsyncSession, expires_in: timedelta) -> str:
    """One invite code (16 random bytes), shown once; only its SHA-256 is stored.
    ``created_by`` is null: this is the CLI path."""
    code, digest = _new_invite_code()
    await db.execute(insert(invites).values(code_hash=digest, expires_at=func.now() + expires_in))
    return code


# --- the admin area's invites (ADR 0011 sections 1 and 6, AD2 #220) ---------------------------
#
# Called with the admin's audited ``admin_session`` on ``ownvitals_maint``, whose column grants
# (``docker/postgres/maint/30-maint.sql``) allow exactly what is used here: never ``code_hash``.


@dataclass(frozen=True, slots=True)
class InviteRow:
    id: UUID
    created_by: UUID | None
    used_by: UUID | None
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    state: str  # active, used or expired


_INVITE_STATE = case(
    (invites.c.used_at.is_not(None), "used"),
    (invites.c.expires_at <= func.now(), "expired"),
    else_="active",
).label("state")
_INVITE_COLUMNS = (
    invites.c.id,
    invites.c.created_by,
    invites.c.used_by,
    invites.c.created_at,
    invites.c.expires_at,
    invites.c.used_at,
    _INVITE_STATE,
)


def _invite(row: Any) -> InviteRow:
    return InviteRow(
        row.id, row.created_by, row.used_by, row.created_at, row.expires_at, row.used_at, row.state
    )


async def list_invites(db: AsyncSession, *, offset: int, limit: int) -> list[InviteRow]:
    """A page of invites, newest first."""
    statement = (
        select(*_INVITE_COLUMNS)
        .order_by(invites.c.created_at.desc(), invites.c.id)
        .offset(offset)
        .limit(limit)
    )
    return [_invite(row) for row in (await db.execute(statement)).all()]


async def get_invite(db: AsyncSession, invite_id: UUID) -> InviteRow | None:
    row = (await db.execute(select(*_INVITE_COLUMNS).where(invites.c.id == invite_id))).first()
    return None if row is None else _invite(row)


async def create_admin_invite(
    db: AsyncSession, expires_in: timedelta, *, invite_id: UUID, created_by: UUID
) -> tuple[str, InviteRow]:
    """The CLI's invite with ``created_by`` set to the admin: the code (shown once) and its row.
    The caller picks ``invite_id``, so its audit row (written first) can name the invite."""
    code, digest = _new_invite_code()
    await db.execute(
        insert(invites).values(
            id=invite_id,
            code_hash=digest,
            created_by=created_by,
            expires_at=func.now() + expires_in,
        )
    )
    row = await get_invite(db, invite_id)
    if row is None:  # pragma: no cover - just inserted in this transaction
        raise RuntimeError("the new invite is not readable")
    return code, row


async def use_invite(db: AsyncSession, invite_id: UUID) -> bool:
    """Consume an invite at sign-up: its row locked (``FOR UPDATE``) and checked unused and
    unexpired, then marked used. ``False`` when it is used, expired or disabled, including by
    an admin disable that committed while this transaction waited for the lock (Postgres
    re-checks the conditions on the row's newest version)."""
    live = await db.execute(
        select(invites.c.id)
        .where(
            invites.c.id == invite_id,
            invites.c.used_at.is_(None),
            invites.c.expires_at > func.now(),
        )
        .with_for_update()
    )
    if live.first() is None:
        return False
    await db.execute(update(invites).where(invites.c.id == invite_id).values(used_at=func.now()))
    return True


async def disable_invite(db: AsyncSession, invite_id: UUID) -> tuple[bool, InviteRow | None]:
    """Expire an unused, unexpired invite, so the consume path (``use_invite``) refuses it.
    ``(changed, row)``; ``row`` is ``None`` when there is no such invite. A used or expired
    invite is left as it is (``changed`` false).

    ``expires_at`` becomes ``created_at``, not ``now()`` (#227, Architect on the #279 review):
    ``now()`` is the transaction's start, so a sign-up confirm that began before the disable
    committed would still see ``expires_at > now()`` for its own, earlier ``now()``.
    ``created_at`` is in the past for every transaction that can know the invite."""
    changed = (
        await db.execute(
            update(invites)
            .where(
                invites.c.id == invite_id,
                invites.c.used_at.is_(None),
                invites.c.expires_at > func.now(),
            )
            .values(expires_at=invites.c.created_at)
            .returning(invites.c.id)
        )
    ).first() is not None
    return changed, await get_invite(db, invite_id)


async def _set_role(db: AsyncSession, user: UserRow, role: str, kind: str) -> None:
    await db.execute(update(users).where(users.c.id == user.id).values(role=role))
    await revoke_user_sessions(db, user.id)
    await record_event(db, kind, user_id=user.id, detail={"source": "cli"})


async def grant_admin(db: AsyncSession, email: str) -> bool:
    """Make an existing user an admin and sign them out everywhere. False if they already were.
    The role only takes effect in a session that passed TOTP (ADR 0003 section 2)."""
    user = await _require_user(db, email)
    if user.role == "admin":
        return False
    await _set_role(db, user, "admin", "admin_granted")
    return True


async def revoke_admin(db: AsyncSession, settings: Settings, email: str) -> bool:
    """Make an admin a plain user and sign them out everywhere. Refused while the address is in
    ``ADMIN_EMAILS``, because the next sign-in would grant it again."""
    user = await _require_user(db, email)
    if user.email_normalized in settings.admin_email_set:
        raise AccountError("this address is in OWNVITALS_ADMIN_EMAILS; remove it there first")
    if user.role != "admin":
        return False
    await _set_role(db, user, "user", "admin_revoked")
    return True


async def grant_admin_if_listed(db: AsyncSession, settings: Settings, user_id: UUID) -> bool:
    """The ``ADMIN_EMAILS`` bootstrap, for sign-in to call before it creates its session.

    A listed address that is not yet an admin becomes one and every session of the user is
    revoked; the caller then gives the sign-in that triggered it a new session. Returns whether
    a grant happened.
    """
    row = (
        await db.execute(
            select(users.c.email_normalized, users.c.role).where(users.c.id == user_id)
        )
    ).first()
    if row is None or row.role == "admin" or row.email_normalized not in settings.admin_email_set:
        return False
    await db.execute(update(users).where(users.c.id == user_id).values(role="admin"))
    await revoke_user_sessions(db, user_id)
    await record_event(db, "admin_granted", user_id=user_id, detail={"source": "admin_emails"})
    return True


async def reset_mfa(db: AsyncSession, email: str) -> UserRow:
    """Lost authenticator: remove the user's TOTP and recovery codes, revoke every session and
    forget their known devices. The caller then sends the password-reset link (``hooks``)."""
    user = await _require_user(db, email)
    await db.execute(delete(mfa_totp).where(mfa_totp.c.user_id == user.id))
    await db.execute(delete(mfa_recovery_codes).where(mfa_recovery_codes.c.user_id == user.id))
    await revoke_user_sessions(db, user.id)
    await forget_devices(db, user.id)
    await record_event(db, "mfa_reset", user_id=user.id, detail={"source": "cli"})
    return user
