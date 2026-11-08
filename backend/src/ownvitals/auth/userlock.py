"""The user-row lock that orders sign-in, linking, reset and the revokes (#248, classbeat #639).

Lock order (``docs/dev/auth.md``, "Lock order"): a transaction that starts a session from a
password or a Google identity, rotates one (``sessions.rotate_session``: MFA verify, TOTP
confirm, step-up, every re-auth, TOTP removal), links an identity, requests or completes a reset,
changes the password or the email, signs out everywhere, resets the second factor, or disables
or deletes an account takes ``SELECT ... FROM users WHERE id = ... FOR NO KEY UPDATE`` as its
**first** lock, then touches sessions, identities, passkeys, TOTP and tokens. Password checks
never run while it is held: the finish re-checks what it proved (the hash, the session, the
identity) under the lock instead.

Why an explicit lock: inserting a session or an identity takes only ``FOR KEY SHARE`` on
``users`` (the foreign key check), which does not conflict with a reset's ``UPDATE users``. A
reset could then commit while a sign-in that checked the old password is still inserting its
session, and the reset's ``DELETE FROM sessions`` would not see that row. With both sides taking
this lock one waits for the other: a finish that commits first is undone by the reset, and one
that waits finds the new hash or its session gone and stops.

Why ``NO KEY UPDATE`` and not ``FOR UPDATE``: two ``NO KEY UPDATE`` locks conflict, which is all
the ordering needs, and it also conflicts with the ``FOR SHARE`` of a passkey or QR sign-in
(``passkeys.lock_user``). It does not block the ``FOR KEY SHARE`` of a foreign-key check, so a
transaction that holds a session row and inserts a row referencing the user (the session
middleware, an ``auth_events`` row) never deadlocks against a holder of this lock that waits for
that session row, as it would with ``FOR UPDATE`` (classbeat #639).

Why every rotation takes it too: a rotation deletes its old row and inserts a new one. A reset's
``DELETE FROM sessions WHERE user_id = ...`` that waits for the old row skips it once the
rotation commits, and never sees the new row, which is not in its snapshot (READ COMMITTED). So
the rotated session would survive; with the lock first the two never overlap.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.tables import sessions, users


@dataclass(frozen=True, slots=True)
class LockedUser:
    password_hash: str | None
    disabled: bool


async def lock_user_row(db: AsyncSession, user_id: UUID) -> LockedUser | None:
    """Lock the user's row ``FOR NO KEY UPDATE`` and read it as committed now; None if gone."""
    row = (
        await db.execute(
            select(users.c.password_hash, users.c.disabled_at)
            .where(users.c.id == user_id)
            .with_for_update(key_share=True)  # FOR NO KEY UPDATE: see the module docstring
        )
    ).first()
    return None if row is None else LockedUser(row.password_hash, row.disabled_at is not None)


async def session_alive(db: AsyncSession, user_id: UUID, session_id: UUID) -> bool:
    """Whether the session still exists for the user and is not past its absolute expiry. Called
    under ``lock_user_row``: a reset or revoke that committed meanwhile deleted it."""
    found = await db.execute(
        select(sessions.c.id).where(
            sessions.c.id == session_id,
            sessions.c.user_id == user_id,
            sessions.c.expires_at > func.now(),
        )
    )
    return found.first() is not None
