"""A user's own sessions: revoke one or all, list the live ones, count them (ADR 0003
section 3). Every function takes the caller's ``AsyncSession`` (a ``system_session``).
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.session_cross_device import idle_cutoff
from ownvitals.auth.session_model import PENDING_WINDOW
from ownvitals.auth.tables import sessions
from ownvitals.config import Settings


async def revoke_session(db: AsyncSession, user_id: UUID, session_id: UUID) -> bool:
    """Delete one of the user's own sessions. False for an unknown id and for another user's,
    which the caller answers with the same 404."""
    result = await db.execute(
        delete(sessions)
        .where(sessions.c.id == session_id, sessions.c.user_id == user_id)
        .returning(sessions.c.id)
    )
    return result.first() is not None


async def revoke_user_sessions(
    db: AsyncSession, user_id: UUID, *, except_id: UUID | None = None
) -> int:
    """Sign out everywhere (or everywhere but ``except_id``). Returns how many were deleted."""
    statement = delete(sessions).where(sessions.c.user_id == user_id)
    if except_id is not None:
        statement = statement.where(sessions.c.id != except_id)
    result = await db.execute(statement.returning(sessions.c.id))
    return len(result.all())


@dataclass(frozen=True, slots=True)
class SessionSummary:
    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    user_agent: str | None
    ip: str | None


def _proof_at_or_before(now: Any) -> Any:
    """The later of ``reauth_at`` and ``stepup_at`` that is not after ``now``.

    A timestamp after ``now`` is not a proof: the same fail-closed lower bound as
    ``session_model._within``. ``GREATEST`` ignores nulls, so a future value dropped by
    ``CASE`` leaves the other timestamp, and both missing is null (the caller uses
    ``created_at``).
    The same expression is in ``purge.purge_expired``.
    """
    return func.greatest(
        case((sessions.c.reauth_at <= now, sessions.c.reauth_at)),
        case((sessions.c.stepup_at <= now, sessions.c.stepup_at)),
    )


def _live_filters(settings: Settings) -> tuple[Any, ...]:
    """Sessions that still count: the same rule as ``sessions.resolve_session``.

    Absolute- and idle-expired rows, and rows that never passed their second factor and are past
    their ten minutes, are left out. A ``reauth_at`` or ``stepup_at`` after now is not a proof.
    """
    now = func.now()
    return (
        sessions.c.expires_at > now,
        sessions.c.last_seen_at > idle_cutoff(settings),  # shorter for a cross-device one
        # a session that never passed its second factor ends ten minutes after its proof
        or_(
            sessions.c.mfa_at.is_not(None),
            # both proofs missing, or only after now, counts from the creation
            func.coalesce(_proof_at_or_before(now), sessions.c.created_at) > now - PENDING_WINDOW,
        ),
    )


async def list_sessions(
    db: AsyncSession, settings: Settings, user_id: UUID
) -> list[SessionSummary]:
    """The user's own live sessions, newest first. Never the token or its hash."""
    rows = await db.execute(
        select(
            sessions.c.id,
            sessions.c.created_at,
            sessions.c.last_seen_at,
            sessions.c.expires_at,
            sessions.c.user_agent,
            sessions.c.ip,
        )
        .where(sessions.c.user_id == user_id, *_live_filters(settings))
        .order_by(sessions.c.created_at.desc())
    )
    return [SessionSummary(*row) for row in rows.all()]


async def count_live_sessions(
    db: AsyncSession, settings: Settings, user_ids: Collection[UUID]
) -> dict[UUID, int]:
    """How many live sessions each id has. The same rows as ``list_sessions``, counted.

    An id with none is absent. Never a token, a hash, an address or an agent string.
    """
    wanted = list(dict.fromkeys(user_ids))
    if not wanted:
        return {}
    counted = func.count().label("n")
    rows = await db.execute(
        select(sessions.c.user_id, counted)
        .where(sessions.c.user_id.in_(wanted), *_live_filters(settings))
        .group_by(sessions.c.user_id)
    )
    return {row.user_id: int(row.n) for row in rows.all()}
