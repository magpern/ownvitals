"""Server-side sessions (ADR 0003 sections 2 and 3).

The cookie holds a random token; the database holds only its SHA-256. Every function takes the
caller's ``AsyncSession`` (a ``system_session``), so a sign-in can create its session in the
same transaction as its other writes. All times are the database's ``now()``.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.keys import new_token, token_hash
from ownvitals.auth.tables import mfa_totp, sessions, users
from ownvitals.config import Settings
from ownvitals.db import system_session

USER_AGENT_LIMIT = 200
TOUCH_INTERVAL = timedelta(minutes=1)
ADMIN_MFA_WINDOW = timedelta(hours=12)
RECENT_AUTH_WINDOW = timedelta(minutes=10)
STEPUP_WINDOW = timedelta(minutes=5)


class SessionGone(Exception):
    """The session to rotate no longer exists (revoked or rotated by a parallel request)."""


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """A valid session joined with its user, as of the database time ``now``."""

    id: UUID
    user_id: UUID
    email: str
    stored_role: Literal["user", "admin"]
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    reauth_at: datetime | None
    mfa_at: datetime | None
    stepup_at: datetime | None
    now: datetime
    totp_enabled: bool = False  # a confirmed TOTP exists: step-up applies (section 6)

    @property
    def role(self) -> Literal["user", "admin"]:
        """``admin`` only for an admin whose session passed TOTP in the last 12 hours."""
        if self.stored_role == "admin" and _within(self.mfa_at, self.now, ADMIN_MFA_WINDOW):
            return "admin"
        return "user"

    @property
    def admin_pending_mfa(self) -> bool:
        """An admin account whose TOTP check is missing or older than 12 hours (``role`` is
        ``user`` meanwhile). False for every other account."""
        return self.stored_role == "admin" and self.role != "admin"

    @property
    def recent_mfa(self) -> bool:
        """TOTP passed within the 12-hour window."""
        return _within(self.mfa_at, self.now, ADMIN_MFA_WINDOW)

    @property
    def recent_auth(self) -> bool:
        return _within(self.reauth_at, self.now, RECENT_AUTH_WINDOW)

    @property
    def recent_stepup(self) -> bool:
        return _within(self.stepup_at, self.now, STEPUP_WINDOW)


def _within(moment: datetime | None, now: datetime, window: timedelta) -> bool:
    return moment is not None and now - moment <= window


@dataclass(frozen=True, slots=True)
class Resolution:
    """``anonymous``: no cookie or an unknown token (the cookie is left alone); ``refused``: a
    known session that expired, idled out or whose user is disabled (deleted; clear the cookie);
    ``ok``: ``info`` is set."""

    outcome: Literal["anonymous", "refused", "ok"]
    info: SessionInfo | None = None


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A new session. ``token`` goes into the cookie once and is never stored or logged."""

    id: UUID
    token: str
    expires_at: datetime
    max_age: int  # remaining absolute lifetime, in seconds


def _truncate(user_agent: str | None) -> str | None:
    return user_agent[:USER_AGENT_LIMIT] if user_agent else None


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
                    users.c.email,
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
        idle = timedelta(days=settings.session_idle_days)
        if row.expires_at <= row.now or row.now - row.last_seen_at >= idle or row.disabled_at:
            await db.execute(delete(sessions).where(sessions.c.id == row.id))
            return Resolution("refused")
        await db.execute(
            update(sessions)
            .where(sessions.c.id == row.id, sessions.c.last_seen_at < func.now() - TOUCH_INTERVAL)
            .values(last_seen_at=func.now())
        )
    return Resolution(
        "ok",
        SessionInfo(
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
        ),
    )


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
    its absolute limit) and its ``mfa_at`` and ``stepup_at``; otherwise it starts clean.
    """
    inherited = None
    if replacing_token:
        # Only a session that is still valid passes anything on: not absolute-expired, not
        # idle-expired, and its user not disabled. The old row is deleted either way.
        idle = timedelta(days=settings.session_idle_days)
        old = (
            await db.execute(
                delete(sessions)
                .where(sessions.c.token_hash == token_hash(replacing_token))
                .returning(
                    sessions.c.user_id,
                    sessions.c.expires_at,
                    sessions.c.mfa_at,
                    sessions.c.stepup_at,
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
                mfa_at=func.now()
                if mfa
                else (inherited.mfa_at if same_user and inherited else None),
                stepup_at=inherited.stepup_at if same_user and inherited else None,
                user_agent=_truncate(user_agent),
                ip=ip,
            )
            .returning(sessions.c.id, sessions.c.expires_at, _remaining())
        )
    ).one()
    return IssuedSession(row.id, token, row.expires_at, max(int(row.remaining), 0))


def _remaining():
    return func.extract("epoch", sessions.c.expires_at - func.now()).label("remaining")


async def rotate_session(
    db: AsyncSession,
    info: SessionInfo,
    *,
    reauth: bool = False,
    mfa: bool = False,
    stepup: bool = False,
    user_agent: str | None = None,
    ip: str | None = None,
) -> IssuedSession:
    """After MFA verification or re-authentication: a new token in a new row, the old row
    deleted, ``expires_at`` unchanged, ``mfa_at``, ``reauth_at`` and ``stepup_at`` carried over
    unless this rotation sets them. No grace period: the old token is dead at once."""
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
                mfa_at=func.now() if mfa else old.mfa_at,
                stepup_at=func.now() if stepup else old.stepup_at,
                user_agent=_truncate(user_agent) or old.user_agent,
                ip=ip or old.ip,
            )
            .returning(sessions.c.id, sessions.c.expires_at, _remaining())
        )
    ).one()
    return IssuedSession(row.id, token, row.expires_at, max(int(row.remaining), 0))


async def clear_mfa(db: AsyncSession, session_id: UUID) -> None:
    """The session no longer counts as having passed TOTP or a step-up (after TOTP was removed)."""
    await db.execute(
        update(sessions).where(sessions.c.id == session_id).values(mfa_at=None, stepup_at=None)
    )


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


async def list_sessions(
    db: AsyncSession, settings: Settings, user_id: UUID
) -> list[SessionSummary]:
    """The user's own live sessions, newest first. Never the token or its hash.

    Absolute- and idle-expired rows that the purge has not removed yet are left out, the same
    rule as ``resolve_session`` (a row whose ``expires_at`` has passed, or whose ``last_seen_at``
    is ``session_idle_days`` or more ago, is not a session).
    """
    idle = timedelta(days=settings.session_idle_days)
    rows = await db.execute(
        select(
            sessions.c.id,
            sessions.c.created_at,
            sessions.c.last_seen_at,
            sessions.c.expires_at,
            sessions.c.user_agent,
            sessions.c.ip,
        )
        .where(
            sessions.c.user_id == user_id,
            sessions.c.expires_at > func.now(),
            sessions.c.last_seen_at > func.now() - idle,
        )
        .order_by(sessions.c.created_at.desc())
    )
    return [SessionSummary(*row) for row in rows.all()]
