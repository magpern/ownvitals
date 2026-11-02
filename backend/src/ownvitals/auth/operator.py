"""The auth side of the operator CLI (ADR 0003 section 1, "Admins"; classbeat ADR 0003 §7).

``python -m ownvitals invite create``, ``admin grant``, ``admin revoke`` and ``admin mfa-reset``
run as the app role through these functions; ``ownvitals.cli`` opens no session itself (only
``ownvitals.db`` and ``ownvitals.auth`` may, ``tests/session_scan.py``). Each runs in one
``system_session``; ``mfa_reset`` mails the reset link after its transaction committed.
"""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from ownvitals.auth import accounts
from ownvitals.auth.devices import forget_devices
from ownvitals.auth.events import record_event
from ownvitals.auth.hooks import register_reset_link_sender, send_reset_link
from ownvitals.auth.reset_routes import make_reset_link_sender
from ownvitals.auth.sessions import revoke_user_sessions
from ownvitals.auth.tables import auth_events, mfa_recovery_codes, mfa_totp, users
from ownvitals.config import Settings
from ownvitals.db import system_session
from ownvitals.mail import Mailer

Factory = async_sessionmaker[AsyncSession]
AccountError = accounts.AccountError


async def create_invite(factory: Factory, expires_in: timedelta) -> str:
    """One invite code, shown once (only its SHA-256 is stored)."""
    async with system_session(factory) as db:
        return await accounts.create_invite(db, expires_in)


async def grant_admin(factory: Factory, email: str) -> bool:
    async with system_session(factory) as db:
        return await accounts.grant_admin(db, email)


async def revoke_admin(factory: Factory, settings: Settings, email: str) -> bool:
    async with system_session(factory) as db:
        return await accounts.revoke_admin(db, settings, email)


async def mfa_reset(factory: Factory, settings: Settings, email: str) -> bool:
    """Remove the user's TOTP, recovery codes and known devices, revoke every session and write
    ``mfa_reset`` (one transaction), then mail a reset link. Returns whether it was mailed;
    raises ``AccountError`` for an unknown address and ``MailError`` when the mail fails (the
    reset itself stays done)."""
    async with system_session(factory) as db:
        user = await accounts.reset_mfa(db, email)
    register_reset_link_sender(make_reset_link_sender(settings, factory, Mailer(settings)))
    try:
        return await send_reset_link(user.id, user.email)
    finally:
        register_reset_link_sender(None)


@dataclass(frozen=True, slots=True, repr=False)
class WipeResult:
    """One wiped account. ``repr`` omits the address."""

    event_id: UUID
    email: str
    providers: tuple[str, ...]
    totp_reset: bool

    def __repr__(self) -> str:
        return f"{type(self).__name__} redacted"


@dataclass(frozen=True, slots=True, repr=False)
class PendingWipe:
    """A wipe whose mail has not been sent. ``repr`` omits the address."""

    event_id: UUID
    email: str
    totp_reset: bool

    def __repr__(self) -> str:
        return f"{type(self).__name__} redacted"


async def user_ids(factory: Factory) -> list[UUID]:
    """Every account, disabled ones included: a wipe and a rewrap visit each of them."""
    async with system_session(factory) as db:
        rows = (await db.execute(select(users.c.id).order_by(users.c.id))).scalars().all()
    return list(rows)


async def app_sessions(factory: Factory, role: str) -> int:
    """Sessions of ``role`` in this database, not counting the session that asks.

    ``keys wipe`` refuses while the app role is still connected. The owner role runs the
    query; it counts backends, not statements.
    """
    async with system_session(factory) as db:
        count = (
            await db.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE usename = :role AND datname = current_database() "
                    "AND pid <> pg_backend_pid()"
                ),
                {"role": role},
            )
        ).scalar_one()
    return int(count)


async def wipe_user(
    db: AsyncSession, user_id: UUID, providers: tuple[str, ...]
) -> WipeResult | None:
    """Reset the authenticator the way ``mfa-reset`` does, and record ``connections_wiped``.

    ``None`` when there is no connection row and no authenticator: nothing was removed.
    ``db`` is the caller's transaction. The mail is sent after that transaction commits.
    """
    had_totp = (
        await db.execute(select(mfa_totp.c.id).where(mfa_totp.c.user_id == user_id))
    ).first()
    if not providers and had_totp is None:
        return None
    removed = (
        await db.execute(
            delete(mfa_totp).where(mfa_totp.c.user_id == user_id).returning(mfa_totp.c.id)
        )
    ).first()
    await db.execute(delete(mfa_recovery_codes).where(mfa_recovery_codes.c.user_id == user_id))
    await revoke_user_sessions(db, user_id)
    await forget_devices(db, user_id)
    totp_reset = removed is not None
    if totp_reset:
        await record_event(db, "mfa_reset", user_id=user_id, detail={"source": "keys_wipe"})
    email = (await db.execute(select(users.c.email).where(users.c.id == user_id))).scalar_one()
    event_id = (
        await db.execute(
            insert(auth_events)
            .values(
                kind="connections_wiped",
                user_id=user_id,
                detail={
                    "providers": list(providers),
                    "totp_reset": totp_reset,
                    "mailed": False,
                },
            )
            .returning(auth_events.c.id)
        )
    ).scalar_one()
    return WipeResult(event_id, email, providers, totp_reset)


async def pending_wipe_mails(factory: Factory) -> list[PendingWipe]:
    """Wipe events whose mail has not been marked sent, for accounts that still exist."""
    mailed = func.coalesce(auth_events.c.detail["mailed"].astext, "false")
    async with system_session(factory) as db:
        rows = (
            await db.execute(
                select(auth_events.c.id, users.c.email, auth_events.c.detail)
                .join(users, users.c.id == auth_events.c.user_id)
                .where(auth_events.c.kind == "connections_wiped", mailed != "true")
                .order_by(auth_events.c.at, auth_events.c.id)
            )
        ).all()
    pending: list[PendingWipe] = []
    for row in rows:
        detail = row.detail if isinstance(row.detail, dict) else {}
        pending.append(PendingWipe(row.id, row.email, bool(detail.get("totp_reset"))))
    return pending


async def mark_wipe_mailed(factory: Factory, event_id: UUID) -> None:
    """Record that this wipe's mail was sent. The detail gains no address and no token."""
    async with system_session(factory) as db:
        row = (
            await db.execute(
                select(auth_events.c.detail)
                .where(auth_events.c.id == event_id, auth_events.c.kind == "connections_wiped")
                .with_for_update()
            )
        ).first()
        if row is None or not isinstance(row.detail, dict):
            return
        detail = dict(row.detail)
        detail["mailed"] = True
        await db.execute(
            update(auth_events).where(auth_events.c.id == event_id).values(detail=detail)
        )


async def vacuum_wiped_auth(connection: AsyncConnection) -> None:
    """Rewrite the authenticator tables after a wipe removed their rows."""
    await connection.execute(text("VACUUM (FULL) public.mfa_totp"))
    await connection.execute(text("VACUUM (FULL) public.mfa_recovery_codes"))
