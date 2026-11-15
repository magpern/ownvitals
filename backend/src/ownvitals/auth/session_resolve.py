"""Resolving a session cookie (ADR 0003 section 2, steps 2 to 4).

Moved out of ``sessions`` unchanged (#248); ``sessions`` re-exports it.
"""

from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.keys import token_hash
from ownvitals.auth.second_factor_policy import totp_required
from ownvitals.auth.session_model import Resolution, SessionInfo, idle_limit
from ownvitals.auth.tables import mfa_totp, sessions, users
from ownvitals.config import Settings
from ownvitals.db import system_session

TOUCH_INTERVAL = timedelta(minutes=1)


async def resolve_session(
    factory: async_sessionmaker[AsyncSession], settings: Settings, token: str | None
) -> Resolution:
    """ADR 0003 section 2, steps 2 to 4."""
    if not token:
        return Resolution("anonymous")
    async with system_session(factory) as db:
        row = (
            await db.execute(
                select(
                    sessions.c.id,
                    sessions.c.user_id,
                    sessions.c.created_at,
                    sessions.c.last_seen_at,
                    sessions.c.expires_at,
                    sessions.c.reauth_at,
                    sessions.c.mfa_at,
                    sessions.c.stepup_at,
                    sessions.c.origin,
                    sessions.c.mfa_by_approval,
                    users.c.email,
                    users.c.email_normalized,
                    users.c.role,
                    users.c.disabled_at,
                    func.now().label("now"),
                    select(mfa_totp.c.id)
                    .where(
                        mfa_totp.c.user_id == sessions.c.user_id,
                        mfa_totp.c.confirmed_at.is_not(None),
                    )
                    .exists()
                    .label("totp_enabled"),
                )
                .join(users, users.c.id == sessions.c.user_id)
                .where(sessions.c.token_hash == token_hash(token))
            )
        ).first()
        if row is None:
            return Resolution("anonymous")  # maybe a token that was just rotated
        info = SessionInfo(
            id=row.id,
            user_id=row.user_id,
            email=row.email,
            stored_role=row.role,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
            reauth_at=row.reauth_at,
            mfa_at=row.mfa_at,
            stepup_at=row.stepup_at,
            now=row.now,
            totp_enabled=row.totp_enabled,
            totp_required=totp_required(row.role, row.email_normalized, settings.admin_email_set),
            stepup_window=timedelta(minutes=settings.stepup_window_minutes),
            origin=row.origin,
            mfa_by_approval=row.mfa_by_approval,
        )
        # A cross-device passkey session idles out sooner (ADR 0011 section 3, 2026-11-08).
        idle = idle_limit(timedelta(hours=settings.session_idle_hours), row.origin)
        if (
            row.expires_at <= row.now
            or row.now - row.last_seen_at >= idle
            or row.disabled_at
            or info.pending_expired
        ):
            await db.execute(delete(sessions).where(sessions.c.id == row.id))
            return Resolution("refused")
        await db.execute(
            update(sessions)
            .where(sessions.c.id == row.id, sessions.c.last_seen_at < func.now() - TOUCH_INTERVAL)
            .values(last_seen_at=func.now())
        )
    return Resolution("ok", info)
