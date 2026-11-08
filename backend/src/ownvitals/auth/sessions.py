"""Server-side sessions (ADR 0003 sections 2 and 3).

The cookie holds a random token; the database holds only its SHA-256. Every function takes the
caller's ``AsyncSession`` (a ``system_session``), so a sign-in can create its session in the
same transaction as its other writes. All times are the database's ``now()``.
"""

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.keys import new_token, token_hash
from ownvitals.auth.session_manage import (  # noqa: F401 - re-exported
    SessionSummary,
    count_live_sessions,
    list_sessions,
    revoke_session,
    revoke_user_sessions,
)
from ownvitals.auth.session_model import (  # noqa: F401 - re-exported
    ADMIN_MFA_WINDOW,
    ADMIN_STEPUP_WINDOW,
    PENDING_WINDOW,
    RECENT_AUTH_WINDOW,
    STEPUP_WINDOW,
    IssuedSession,
    Resolution,
    SessionGone,
    SessionInfo,
)
from ownvitals.auth.session_resolve import TOUCH_INTERVAL, resolve_session  # noqa: F401
from ownvitals.auth.tables import sessions, users
from ownvitals.config import Settings

USER_AGENT_LIMIT = 200


def _truncate(user_agent: str | None) -> str | None:
    return user_agent[:USER_AGENT_LIMIT] if user_agent else None


async def start_session(
    db: AsyncSession,
    settings: Settings,
    user_id: UUID,
    *,
    user_agent: str | None,
    ip: str | None,
    replacing_token: str | None = None,
    mfa: bool = False,
) -> IssuedSession:
    """Sign-in: a new token in a new row, with the browser's old session (if any) deleted.

    The sign-in itself is a fresh authentication, so ``reauth_at`` is now. When the old session
    was this user's, the new row keeps its ``expires_at`` (rotation never extends a session past
    its absolute limit). A sign-in never carries a second factor over: ``mfa_at`` and
    ``stepup_at`` start empty, so every sign-in passes TOTP again (ownvitals ADR 0003 section 2).
    ``mfa`` is for tests and the operator tooling only; no route passes it.
    """
    inherited = None
    if replacing_token:
        # Only a session that is still valid passes anything on: not absolute-expired, not
        # idle-expired, and its user not disabled. The old row is deleted either way.
        idle = timedelta(hours=settings.session_idle_hours)
        old = (
            await db.execute(
                delete(sessions)
                .where(sessions.c.token_hash == token_hash(replacing_token))
                .returning(
                    sessions.c.user_id,
                    sessions.c.expires_at,
                    (sessions.c.expires_at > func.now()).label("absolute_ok"),
                    (sessions.c.last_seen_at > func.now() - idle).label("idle_ok"),
                    select(users.c.disabled_at.is_(None))
                    .where(users.c.id == sessions.c.user_id)
                    .scalar_subquery()
                    .label("enabled"),
                )
            )
        ).first()
        if (
            old is not None
            and old.user_id == user_id
            and old.absolute_ok
            and old.idle_ok
            and old.enabled
        ):
            inherited = old
    same_user = inherited is not None  # only this user's own, still valid session passes on
    token = new_token()
    now = func.now()
    row = (
        await db.execute(
            insert(sessions)
            .values(
                user_id=user_id,
                token_hash=token_hash(token),
                expires_at=inherited.expires_at
                if same_user and inherited
                else now + timedelta(days=settings.session_absolute_days),
                reauth_at=now,
                mfa_at=func.now() if mfa else None,
                stepup_at=None,
                user_agent=_truncate(user_agent),
                ip=ip,
            )
            .returning(sessions.c.id, sessions.c.expires_at, _remaining())
        )
    ).one()
    return IssuedSession(row.id, token, row.expires_at, max(int(row.remaining), 0))


def _remaining():
    return func.extract("epoch", sessions.c.expires_at - func.now()).label("remaining")


async def start_approved_session(
    db: AsyncSession,
    settings: Settings,
    user_id: UUID,
    *,
    approved_at: datetime,
    factor: Literal["totp", "recovery_code"],
    approved_by: UUID,
    user_agent: str | None,
    ip: str | None,
    replacing_token: str | None = None,
) -> IssuedSession:
    """The desktop's session after a QR approval (ADR 0011 section 5): a new token in a new row,
    the browser's old session (if any) deleted and nothing carried over from it.

    It is full: ``mfa_at`` is the approval time, because a TOTP or recovery code was entered on
    the phone for this approval (amending ADR 0003 section 2); ``mfa_by_approval`` keeps that
    code from counting for the admin role. ``reauth_at`` and ``stepup_at`` are empty, so every
    sensitive or step-up action on the desktop needs its own proofs. ``origin`` is
    ``qr_approval`` for the life of the session: it can never approve another QR login.
    """
    if replacing_token:
        await db.execute(
            delete(sessions).where(sessions.c.token_hash == token_hash(replacing_token))
        )
    token = new_token()
    row = (
        await db.execute(
            insert(sessions)
            .values(
                user_id=user_id,
                token_hash=token_hash(token),
                expires_at=func.now() + timedelta(days=settings.session_absolute_days),
                reauth_at=None,
                mfa_at=approved_at,
                stepup_at=None,
                user_agent=_truncate(user_agent),
                ip=ip,
                origin="qr_approval",
                mfa_by_approval=True,
                approved_factor=factor,
                approved_by_session_id=approved_by,
            )
            .returning(sessions.c.id, sessions.c.expires_at, _remaining())
        )
    ).one()
    return IssuedSession(row.id, token, row.expires_at, max(int(row.remaining), 0))


async def rotate_session(
    db: AsyncSession,
    info: SessionInfo,
    *,
    reauth: bool = False,
    mfa: bool = False,
    stepup: bool = False,
    drop_mfa: bool = False,
    user_agent: str | None = None,
    ip: str | None = None,
) -> IssuedSession:
    """After MFA verification, re-authentication, a step-up or a privilege change: a new token
    in a new row, the old row deleted, ``expires_at`` unchanged, ``mfa_at``, ``reauth_at`` and
    ``stepup_at`` carried over unless this rotation sets them. ``drop_mfa`` (the TOTP was
    removed) clears ``mfa_at``, so the session goes back to enrolment. No grace period: the old
    token is dead at once."""
    old = (
        await db.execute(
            delete(sessions)
            .where(sessions.c.id == info.id, sessions.c.user_id == info.user_id)
            .returning(
                sessions.c.created_at,
                sessions.c.expires_at,
                sessions.c.reauth_at,
                sessions.c.mfa_at,
                sessions.c.stepup_at,
                sessions.c.user_agent,
                sessions.c.ip,
                sessions.c.origin,
                sessions.c.mfa_by_approval,
                sessions.c.approved_factor,
                sessions.c.approved_by_session_id,
            )
        )
    ).first()
    if old is None:
        raise SessionGone
    token = new_token()
    row = (
        await db.execute(
            insert(sessions)
            .values(
                user_id=info.user_id,
                token_hash=token_hash(token),
                created_at=old.created_at,
                expires_at=old.expires_at,
                reauth_at=func.now() if reauth else old.reauth_at,
                mfa_at=None if drop_mfa else (func.now() if mfa else old.mfa_at),
                stepup_at=func.now() if stepup else old.stepup_at,
                user_agent=_truncate(user_agent) or old.user_agent,
                ip=ip or old.ip,
                # A QR-born session stays one; a code verified in it (``mfa``) is its own.
                origin=old.origin,
                mfa_by_approval=old.mfa_by_approval and not (mfa or drop_mfa),
                approved_factor=old.approved_factor,
                approved_by_session_id=old.approved_by_session_id,
            )
            .returning(sessions.c.id, sessions.c.expires_at, _remaining())
        )
    ).one()
    return IssuedSession(row.id, token, row.expires_at, max(int(row.remaining), 0))
