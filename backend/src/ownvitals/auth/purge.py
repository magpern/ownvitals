"""The hourly purge (ADR 0003 section 1). The worker calls ``purge_expired``."""

from datetime import timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.deps import purge_contexts
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
from ownvitals.context import UserContext
from ownvitals.db import system_session, user_session

RETENTION_DAYS = 30
DEVICE_RETENTION_DAYS = 90
# The longest rate-limit window is an hour; older windows can never be hit again.
RATE_LIMIT_KEEP = timedelta(hours=2)
# ADR 0009 section 3: a Spotify connect flow is deleted when used or expired. The table is owned
# (row security), so it is purged per user in that user's own session.
_SPOTIFY_FLOWS = text(
    "DELETE FROM spotify_oauth_flows WHERE owner_id = :owner_id "
    "AND (expires_at <= now() OR used_at IS NOT NULL)"
)


def _ago(days: int) -> Any:
    return func.now() - timedelta(days=days)


async def purge_expired(
    factory: async_sessionmaker[AsyncSession], settings: Settings
) -> dict[str, int]:
    """Delete what is expired or used; returns the number of rows removed per table.

    - ``sessions``: past their absolute expiry, or idle longer than the idle limit; their ``ip``
      is cleared after 30 days;
    - ``pending_signups``, ``auth_tokens`` (also used ones), ``oauth_flows``: expired;
    - ``rate_limits``: windows over two hours old;
    - ``invites``: used or expired more than 30 days ago;
    - ``known_devices``: unused for 90 days;
    - ``auth_events``: older than 30 days;
    - ``spotify_oauth_flows``: expired or used (an owned table: per user, in their own session).
    """
    idle = timedelta(days=settings.session_idle_days)
    now = func.now()
    statements = {
        "sessions": delete(sessions).where(
            or_(sessions.c.expires_at <= now, sessions.c.last_seen_at <= now - idle)
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
        contexts = await purge_contexts(db)
    counts["spotify_oauth_flows"] = await _purge_spotify_flows(factory, contexts)
    return counts


async def _purge_spotify_flows(
    factory: async_sessionmaker[AsyncSession], contexts: list[UserContext]
) -> int:
    removed = 0
    for ctx in contexts:
        async with user_session(factory, ctx) as db:
            result = await db.execute(_SPOTIFY_FLOWS, {"owner_id": ctx.user_id})
            removed += int(result.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue]
    return removed
