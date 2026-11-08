"""Cross-device passkey sessions (ADR 0011 section 3, amendment of 2026-11-08; #252).

A passkey sign-in whose authenticator is not this device's own (a phone over the hybrid
transport, "Use a phone", or a security key) may be on a shared computer. Its session is marked
``origin = 'passkey_cross_device'`` and gets ``session_model.CROSS_DEVICE_ABSOLUTE`` and
``CROSS_DEVICE_IDLE`` (or the settings' limits, if shorter). Rotation carries ``origin`` and
``expires_at`` over unchanged (``sessions.rotate_session``), so every later rotation keeps them.

``mark_cross_device`` runs in the sign-in's own transaction, right after ``start_session``.
``idle_cutoff`` is the per-row idle rule in SQL, for the session list and the purge, so they
agree with ``session_resolve``.
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import case, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.session_model import (
    CROSS_DEVICE,
    CROSS_DEVICE_ABSOLUTE,
    IssuedSession,
    idle_limit,
)
from ownvitals.auth.tables import sessions
from ownvitals.config import Settings


async def mark_cross_device(db: AsyncSession, issued: IssuedSession) -> IssuedSession:
    """``issued`` marked cross-device, its ``expires_at`` at most ``CROSS_DEVICE_ABSOLUTE``
    from now (never later than it was: a sign-in that inherited a shorter expiry keeps it).
    The token is unchanged."""
    row = (
        await db.execute(
            update(sessions)
            .where(sessions.c.id == issued.id)
            .values(
                origin=CROSS_DEVICE,
                expires_at=func.least(sessions.c.expires_at, func.now() + CROSS_DEVICE_ABSOLUTE),
            )
            .returning(
                sessions.c.expires_at,
                func.extract("epoch", sessions.c.expires_at - func.now()).label("remaining"),
            )
        )
    ).one()
    return IssuedSession(issued.id, issued.token, row.expires_at, max(int(row.remaining), 0))


def idle_cutoff(settings: Settings) -> Any:
    """``now()`` minus each row's idle limit: a session last seen at or before it idled out."""
    configured = timedelta(hours=settings.session_idle_hours)
    limit = case(
        (sessions.c.origin == CROSS_DEVICE, idle_limit(configured, CROSS_DEVICE)),
        else_=configured,
    )
    return func.now() - limit
