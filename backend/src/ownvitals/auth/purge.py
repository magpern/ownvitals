"""The hourly purge (ADR 0003 section 1) and account deletion (PLAN section 4.3).

The worker calls ``purge_expired``. Account deletion and the "also remove imported
rows" option call the other functions here, after the Withings revoke. Owned rows
go with the account because each one cascades from it. Imported rows can also be
removed on their own; the Withings row itself stays until the account goes.
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Column, MetaData, Table, and_, case, delete, func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.admin_accounts import delete_user_account
from ownvitals.auth.tables import (
    auth_events,
    auth_tokens,
    invites,
    known_devices,
    oauth_flows,
    pending_signups,
    rate_limits,
    sessions,
)
from ownvitals.config import Settings
from ownvitals.connections.repository import list_connections
from ownvitals.context import UserContext
from ownvitals.crypto.keks import KeyRing
from ownvitals.db import system_session, user_session
from ownvitals.ingest.tables import data_revisions, sync_cursors
from ownvitals.withings.disconnect import DisconnectRefused, DisconnectResult, disconnect_withings
from ownvitals.withings.errors import RetryBudget
from ownvitals.withings.tables import (
    activity_daily,
    devices,
    ecg_recordings,
    intraday,
    measure_groups,
    measures,
    sleep_series,
    sleep_summaries,
    workouts,
)

# Owned tables that do not cascade from the account and must be removed by name.
# Empty: every owned table cascades, which the account-deletion test enumerates.
EXPLICIT_PURGE: tuple[str, ...] = ()
_PROVIDER = "withings"
# A narrow handle for the queue table, which has no Core table in this package.
_JOBS = Table("jobs", MetaData(), Column("owner_id"))
# Children before parents. Jobs last, so a failure leaves the queue row to retry from.
_IMPORTED = (
    measures,
    measure_groups,
    devices,
    ecg_recordings,
    sleep_summaries,
    activity_daily,
    workouts,
    sleep_series,
    intraday,
    sync_cursors,
    data_revisions,
    _JOBS,
)

RETENTION_DAYS = 30
DEVICE_RETENTION_DAYS = 90
# The longest rate-limit window is an hour; older windows can never be hit again.
RATE_LIMIT_KEEP = timedelta(hours=2)
# ``sessions.PENDING_WINDOW``, repeated so the worker does not load the session module (a test
# holds them equal).
PENDING_WINDOW = timedelta(minutes=10)


def _ago(days: int) -> Any:
    return func.now() - timedelta(days=days)


def _proof_at_or_before(now: Any) -> Any:
    """The later of ``reauth_at`` and ``stepup_at`` that is not after ``now``.

    A timestamp after ``now`` is not a proof: the same fail-closed lower bound as
    ``sessions._within``. ``GREATEST`` ignores nulls, so a future value dropped by ``CASE``
    leaves the other timestamp, and both missing is null (the caller uses ``created_at``).
    The same expression is in ``sessions.list_sessions``.
    """
    return func.greatest(
        case((sessions.c.reauth_at <= now, sessions.c.reauth_at)),
        case((sessions.c.stepup_at <= now, sessions.c.stepup_at)),
    )


async def purge_expired(
    factory: async_sessionmaker[AsyncSession], settings: Settings
) -> dict[str, int]:
    """Delete what is expired or used; returns the number of rows removed per table.

    - ``sessions``: past their absolute expiry, idle longer than the idle limit, or without a
      second factor ten minutes after their latest proof (a ``reauth_at`` or ``stepup_at``
      after now is not a proof); their ``ip`` is cleared after 30 days;
    - ``pending_signups``, ``auth_tokens`` (also used ones), ``oauth_flows``: expired;
    - ``rate_limits``: windows over two hours old;
    - ``invites``: used or expired more than 30 days ago;
    - ``known_devices``: unused for 90 days;
    - ``auth_events``: older than 30 days.
    """
    idle = timedelta(hours=settings.session_idle_hours)
    now = func.now()
    statements = {
        "sessions": delete(sessions).where(
            or_(
                sessions.c.expires_at <= now,
                sessions.c.last_seen_at <= now - idle,
                # never passed its second factor, ten minutes after its latest proof.
                # A timestamp after now does not count.
                and_(
                    sessions.c.mfa_at.is_(None),
                    func.coalesce(_proof_at_or_before(now), sessions.c.created_at)
                    <= now - PENDING_WINDOW,
                ),
            )
        ),
        "pending_signups": delete(pending_signups).where(pending_signups.c.expires_at <= now),
        "auth_tokens": delete(auth_tokens).where(
            or_(auth_tokens.c.expires_at <= now, auth_tokens.c.used_at.is_not(None))
        ),
        "oauth_flows": delete(oauth_flows).where(oauth_flows.c.expires_at <= now),
        "rate_limits": delete(rate_limits).where(
            rate_limits.c.window_start < now - RATE_LIMIT_KEEP
        ),
        "invites": delete(invites).where(
            or_(
                invites.c.used_at < _ago(RETENTION_DAYS),
                and_(invites.c.used_at.is_(None), invites.c.expires_at < _ago(RETENTION_DAYS)),
            )
        ),
        "known_devices": delete(known_devices).where(
            known_devices.c.last_used_at < _ago(DEVICE_RETENTION_DAYS)
        ),
        "auth_events": delete(auth_events).where(auth_events.c.at < _ago(RETENTION_DAYS)),
    }
    counts: dict[str, int] = {}
    async with system_session(factory) as db:
        for name, statement in statements.items():
            result = await db.execute(statement)
            counts[name] = int(result.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue]
        cleared = await db.execute(
            update(sessions)
            .where(sessions.c.ip.is_not(None), sessions.c.created_at < _ago(RETENTION_DAYS))
            .values(ip=None)
        )
        counts["sessions_ip_cleared"] = int(cleared.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue]
    return counts


async def purge_imported(factory: async_sessionmaker[AsyncSession], ctx: UserContext) -> None:
    """Remove this user's imported rows. The Withings row stays.

    The session is this user's, and every statement names their id, so another
    user's rows are left alone. The queue's events follow their job.
    """
    async with user_session(factory, ctx) as db:
        for table in _IMPORTED:
            await db.execute(delete(table).where(table.c.owner_id == ctx.user_id))


async def finish_disconnect(
    factory: async_sessionmaker[AsyncSession],
    ring: KeyRing,
    ctx: UserContext,
    connection_id: UUID,
    *,
    stepped_up: bool,
    delete_imported: bool = False,
    inner: object = None,
    budget: RetryBudget | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> DisconnectResult:
    """Revoke and clear credentials, then optionally remove imported rows."""
    result = await disconnect_withings(
        factory,
        ring,
        ctx,
        connection_id,
        stepped_up=stepped_up,
        inner=inner,
        budget=budget,
        sleep=sleep,
    )
    if delete_imported:
        await purge_imported(factory, ctx)
    return result


async def delete_owner(
    factory: async_sessionmaker[AsyncSession],
    ring: KeyRing,
    ctx: UserContext,
    *,
    stepped_up: bool,
    ip: str | None = None,
    inner: object = None,
    budget: RetryBudget | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> DisconnectResult:
    """Revoke a live Withings grant, then delete the account.

    The audit row is the account-deleted event: no measurement and no credential.
    Owned rows cascade with the account. A missing step-up deletes nothing.
    """
    if stepped_up is not True:
        raise DisconnectRefused("step_up_required")
    rows = await list_connections(factory, ctx)
    result = DisconnectResult(False, 0, False, False, None)
    for row in rows:
        if row.provider == _PROVIDER and row.status != "disconnected":
            result = await disconnect_withings(
                factory,
                ring,
                ctx,
                row.id,
                stepped_up=True,
                inner=inner,
                budget=budget,
                sleep=sleep,
            )
    async with system_session(factory) as db:
        await delete_user_account(db, ctx.user_id, ip=ip)
    return result
