"""The hourly purge (ADR 0003 section 1). The worker calls ``purge_expired``."""

from datetime import timedelta
from typing import Any

from sqlalchemy import and_, case, delete, func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from ownvitals.db import system_session

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
