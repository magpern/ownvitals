"""The auth side of ``python -m ownvitals keys ...`` (ADR 0004 section 2). Used only by the CLI.

- ``operator_contexts`` (in ``deps``, the web issuer): one ``UserContext`` per user, for the
  CLI's per-owner work (``keys rewrap`` as the app role, ``keys wipe`` as the owner role).
- ``wipe_user``: in the caller's per-user transaction, write ``connections_wiped`` (detail
  ``{"providers": [...], "totp_reset": bool}``) and, only if the user has a TOTP row, reset it the
  way ``admin mfa-reset`` does. A user with no connection and no TOTP gets no event.
- ``pending_wipe_mails``, ``wipe_mail`` and ``mark_wipe_mailed``: the mail is built from the event
  after the commit; a sent one gets ``"mailed": true``; ``--resend`` reads the unmarked ones.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from ownvitals.auth import mails
from ownvitals.auth.devices import forget_devices
from ownvitals.auth.events import record_event
from ownvitals.auth.sessions import revoke_user_sessions
from ownvitals.auth.tables import auth_events, auth_tokens, mfa_recovery_codes, mfa_totp, users
from ownvitals.auth.tokens import RESET_LIFETIME, issue_token
from ownvitals.config import Settings
from ownvitals.mail import MailMessage

WIPED = "connections_wiped"
RESET_PURPOSE = "reset_password"


@dataclass(frozen=True, slots=True)
class WipeEvent:
    """A ``connections_wiped`` event: what the mail says. No token, id or account detail."""

    event_id: UUID
    user_id: UUID
    email: str
    providers: tuple[str, ...]
    totp_reset: bool


async def wipe_user(db: AsyncSession, user_id: UUID, providers: list[str]) -> WipeEvent | None:
    """Record the wipe of ``providers`` and reset the user's TOTP if there is one. ``None`` (and
    nothing written) when the user had neither."""
    has_totp = (
        await db.execute(select(mfa_totp.c.id).where(mfa_totp.c.user_id == user_id))
    ).first() is not None
    if not providers and not has_totp:
        return None
    email = (await db.execute(select(users.c.email).where(users.c.id == user_id))).scalar_one()
    if has_totp:
        await db.execute(delete(mfa_totp).where(mfa_totp.c.user_id == user_id))
        await db.execute(delete(mfa_recovery_codes).where(mfa_recovery_codes.c.user_id == user_id))
        await revoke_user_sessions(db, user_id)
        await forget_devices(db, user_id)
        await record_event(db, "mfa_reset", user_id=user_id, detail={"source": "keys_wipe"})
    detail = {"providers": sorted(providers), "totp_reset": has_totp}
    event_id = (
        await db.execute(
            auth_events.insert()
            .values(kind=WIPED, user_id=user_id, detail=detail)
            .returning(auth_events.c.id)
        )
    ).scalar_one()
    return WipeEvent(event_id, user_id, email, tuple(sorted(providers)), has_totp)


async def pending_wipe_mails(db: AsyncSession) -> list[WipeEvent]:
    """``connections_wiped`` events of existing users without the ``mailed`` mark."""
    rows = await db.execute(
        select(auth_events.c.id, auth_events.c.user_id, users.c.email, auth_events.c.detail)
        .join(users, users.c.id == auth_events.c.user_id)
        .where(
            auth_events.c.kind == WIPED,
            func.coalesce(auth_events.c.detail["mailed"].as_boolean(), False).is_(False),
        )
        .order_by(auth_events.c.at, auth_events.c.id)
    )
    events: list[WipeEvent] = []
    for row in rows:
        detail = row.detail or {}
        providers = tuple(str(p) for p in detail.get("providers", []))
        events.append(
            WipeEvent(row.id, row.user_id, row.email, providers, bool(detail.get("totp_reset")))
        )
    return events


async def wipe_mail(settings: Settings, db: AsyncSession, event: WipeEvent) -> MailMessage:
    """The mail for ``event``. With a TOTP reset it holds a password-reset link: a fresh token,
    and the user's earlier outstanding reset tokens stop working (one valid link at a time)."""
    token = None
    if event.totp_reset:
        await db.execute(
            delete(auth_tokens).where(
                auth_tokens.c.user_id == event.user_id,
                auth_tokens.c.purpose == RESET_PURPOSE,
                auth_tokens.c.used_at.is_(None),
            )
        )
        token = await issue_token(db, RESET_PURPOSE, event.user_id, RESET_LIFETIME)
    return mails.connections_wiped_notice(settings, event.email, event.providers, token)


async def mark_wipe_mailed(db: AsyncSession, event_id: UUID) -> None:
    await db.execute(
        update(auth_events)
        .where(auth_events.c.id == event_id, auth_events.c.kind == WIPED)
        .values(detail=auth_events.c.detail.op("||")(func.jsonb_build_object("mailed", True)))
    )


async def vacuum_wiped_tables(connection: AsyncConnection) -> None:
    """``VACUUM (FULL)`` of the TOTP tables after a wipe (autocommit connection, owner role)."""
    for table in ("mfa_totp", "mfa_recovery_codes"):
        await connection.execute(text(f"VACUUM (FULL) public.{table}"))
