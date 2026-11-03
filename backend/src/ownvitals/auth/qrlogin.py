"""QR approve-login: the request rows and their state machine (ADR 0011 sections 5 and 7).

A desktop without a session starts a request bound to its pre-session (section 4) and shows a QR
holding a 32-byte nonce; a phone with a full session scans it, sees the desktop's browser, OS, IP
and place, and approves with a TOTP or recovery code entered **for this approval**, or denies;
the desktop then completes and gets a new, full session of the approving user.

``qr_logins`` is an account system table (guard 8): the desktop has no user until a phone
approves, so the row is not owned and is reached only through this module. Every transition is
one conditional ``UPDATE`` on the row's state, its binding and its deadline, so a second scan, a
second or late approval, a deny racing an approval and a second completion each find no row and
get the one generic refusal. Deadlines: 60 s to scan, 120 s to approve, 30 s to complete, all
the database's ``now()``.

Lock order (ADR 0011 section 3, the #165 item): ``approve`` and ``complete`` lock the account row
(``passkeys.lock_user``, ``FOR SHARE``) before anything else, as a passkey sign-in does, and
``accounts.reset_mfa`` locks it ``FOR UPDATE`` before it expires this account's rows and revokes
its sessions. A ``complete`` that waits for a reset then finds its row expired; one that commits
first sees its new session revoked by the reset. Either way no live session is left.

Each transition sends ``pg_notify('qr_logins', <row id>)``: the row id only (T27), which wakes the
desktop's stream (``qrlogin_stream``). Nothing here logs; the nonce is stored only as its SHA-256
and never reaches a log, an ``auth_events`` row or an error message (section 9).
"""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import and_, func, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth import mfa
from ownvitals.auth.events import record_event
from ownvitals.auth.keys import Keys
from ownvitals.auth.passkeys import b64url_decode, lock_user
from ownvitals.auth.sessions import IssuedSession, SessionInfo, start_approved_session
from ownvitals.auth.tables import mfa_totp, qr_logins, sessions, users
from ownvitals.auth.totp_keys import TotpUnreadable
from ownvitals.config import Settings
from ownvitals.crypto.keks import KeyRing

SCAN_WINDOW: Final = timedelta(seconds=60)
APPROVE_WINDOW: Final = timedelta(seconds=120)
COMPLETE_WINDOW: Final = timedelta(seconds=30)
# The longest a request can live: scan, approve and complete windows end to end (3.5 minutes).
LONGEST: Final = SCAN_WINDOW + APPROVE_WINDOW + COMPLETE_WINDOW
NONCE_BYTES: Final = 32
NONCE_TEXT: Final = 43  # base64url of 32 bytes, without padding
CHANNEL: Final = "qr_logins"
IP_MAX: Final = 64

State = Literal["pending", "scanned", "approved", "completed", "denied", "expired"]
LIVE: Final = ("pending", "scanned", "approved")
ENDED: Final = frozenset({"approved", "completed", "denied", "expired"})  # the stream stops
FACTORS: Final[dict[str, Literal["totp", "recovery_code"]]] = {
    "totp": "totp",
    "recovery": "recovery_code",
}

_NOTIFY = text("SELECT pg_notify('qr_logins', :row_id)")


@dataclass(frozen=True, slots=True)
class Desktop:
    """What the phone is shown about the desktop: its IP, the browser and OS its
    ``User-Agent`` claims (``useragent``), and its place (``geo``; ``None`` is unknown)."""

    ip: str | None
    browser: str
    os: str
    place: str | None


@dataclass(frozen=True, slots=True)
class Scanned:
    id: UUID
    desktop: Desktop
    requested_at: datetime
    approve_deadline: datetime
    now: datetime


@dataclass(frozen=True, slots=True)
class Approval:
    outcome: Literal["approved", "unavailable", "invalid_code"]
    mail_failures: bool = False  # the fifth failed code in an hour: mail the user


@dataclass(frozen=True, slots=True)
class Completed:
    issued: IssuedSession
    user_id: UUID
    email: str
    role: str
    desktop: Desktop


@dataclass(frozen=True, slots=True)
class StreamState:
    state: State
    seconds_left: float  # until the current state's deadline (0 once ended)


def nonce_hash(nonce: bytes) -> bytes:
    return hashlib.sha256(nonce).digest()


def encode_nonce(nonce: bytes) -> str:
    return base64.urlsafe_b64encode(nonce).rstrip(b"=").decode()


def decode_nonce(value: str) -> bytes | None:
    """The 32-byte nonce of a base64url text, or ``None`` for anything else."""
    if len(value) != NONCE_TEXT:
        return None
    try:
        nonce = b64url_decode(value)
    except ValueError:
        return None
    return nonce if len(nonce) == NONCE_BYTES else None


def qr_url(settings: Settings, nonce: bytes) -> str:
    """What the QR encodes: the nonce in the fragment, so it never reaches a server, a proxy log
    or a ``Referer`` (section 5, step 2)."""
    return f"{settings.public_base_url}/qr#{encode_nonce(nonce)}"


async def _notify(db: AsyncSession, row_id: UUID) -> None:
    await db.execute(_NOTIFY, {"row_id": str(row_id)})


def _desktop(row: Any) -> Desktop:
    return Desktop(row.desktop_ip, row.desktop_browser, row.desktop_os, row.desktop_place)


# --- expiry -------------------------------------------------------------------------------------


def _overdue() -> Any:
    """Live rows that can no longer move: past the deadline of their state, bound to no
    pre-session any more (the desktop signed in otherwise, or its pre-session expired), or whose
    approving session is gone (revoked, signed out or rotated away)."""
    state, now = qr_logins.c.state, func.now()
    return and_(
        state.in_(LIVE),
        or_(
            and_(state == "pending", qr_logins.c.scan_deadline <= now),
            and_(state == "scanned", qr_logins.c.approve_deadline <= now),
            and_(state == "approved", qr_logins.c.complete_deadline <= now),
            qr_logins.c.presession_id.is_(None),
            and_(state.in_(("scanned", "approved")), qr_logins.c.approver_session_id.is_(None)),
        ),
    )


async def _expire(db: AsyncSession, *conditions: Any) -> int:
    """Set the matching live rows ``expired``; a row that was scanned writes
    ``qr_login_expired`` for its approver (an unscanned row has no user to attribute it to)."""
    rows = (
        await db.execute(
            update(qr_logins)
            .where(qr_logins.c.state.in_(LIVE), *conditions)
            .values(state="expired")
            .returning(qr_logins.c.id, qr_logins.c.approver_user_id)
        )
    ).all()
    for row in rows:
        await _notify(db, row.id)
        if row.approver_user_id is not None:
            await record_event(
                db, "qr_login_expired", user_id=row.approver_user_id, detail={"qr": str(row.id)}
            )
    return len(rows)


async def expire_overdue(db: AsyncSession, row_id: UUID | None = None) -> int:
    """Expire the overdue rows (one, or all of them for the purge)."""
    conditions = [_overdue()]
    if row_id is not None:
        conditions.append(qr_logins.c.id == row_id)
    return await _expire(db, *conditions)


async def expire_for_account(db: AsyncSession, user_id: UUID) -> int:
    """Account recovery (ADR 0011 section 3): every live row this account scanned or approved.
    The caller holds the account row (``passkeys.lock_user``)."""
    return await _expire(db, qr_logins.c.approver_user_id == user_id)


# --- the desktop --------------------------------------------------------------------------------


async def start(db: AsyncSession, presession_id: UUID, desktop: Desktop) -> tuple[UUID, bytes]:
    """A new request bound to the pre-session; its earlier live request is expired in the same
    transaction (the "new code" button, the 60 s refresh). Returns the row id and the nonce."""
    await _expire(db, qr_logins.c.presession_id == presession_id)
    nonce = secrets.token_bytes(NONCE_BYTES)
    row_id = (
        await db.execute(
            insert(qr_logins)
            .values(
                nonce_hash=nonce_hash(nonce),
                presession_id=presession_id,
                scan_deadline=func.now() + SCAN_WINDOW,
                desktop_ip=(desktop.ip or "")[:IP_MAX] or None,
                desktop_browser=desktop.browser,
                desktop_os=desktop.os,
                desktop_place=desktop.place,
            )
            .returning(qr_logins.c.id)
        )
    ).scalar_one()
    return row_id, nonce


async def is_bound(db: AsyncSession, row_id: UUID, presession_id: UUID | None) -> bool:
    """Whether the row exists and belongs to this pre-session (the stream's 404 check)."""
    if presession_id is None:
        return False
    found = await db.execute(
        select(qr_logins.c.id).where(
            qr_logins.c.id == row_id, qr_logins.c.presession_id == presession_id
        )
    )
    return found.first() is not None


async def stream_state(db: AsyncSession, row_id: UUID, presession_id: UUID) -> StreamState | None:
    """The row's state for the desktop's stream, after expiring it if it is overdue. ``None``
    when it is gone or no longer this pre-session's."""
    await expire_overdue(db, row_id)
    deadline = func.coalesce(
        qr_logins.c.complete_deadline, qr_logins.c.approve_deadline, qr_logins.c.scan_deadline
    )
    row = (
        await db.execute(
            select(
                qr_logins.c.state,
                func.greatest(func.extract("epoch", deadline - func.now()), 0).label("left"),
            ).where(qr_logins.c.id == row_id, qr_logins.c.presession_id == presession_id)
        )
    ).first()
    if row is None:
        return None
    left = 0.0 if row.state not in LIVE else float(row.left)
    return StreamState(row.state, left)


async def complete(
    db: AsyncSession,
    settings: Settings,
    row_id: UUID,
    presession_id: UUID | None,
    *,
    user_agent: str | None,
    ip: str | None,
    replacing_token: str | None,
) -> Completed | None:
    """Take an approved row bound to this pre-session and give the desktop its session; ``None``
    for every refusal (the generic answer). The account row is locked first (lock order above);
    the approver's account must be active with a confirmed TOTP and the approving session still
    there (ADR 0011 section 5, "Account state")."""
    if presession_id is None:
        return None
    bound = (qr_logins.c.id == row_id, qr_logins.c.presession_id == presession_id)
    approver = (
        await db.execute(
            select(qr_logins.c.approver_user_id).where(*bound, qr_logins.c.state == "approved")
        )
    ).scalar_one_or_none()
    if approver is None:
        return None
    user = await lock_user(db, approver, share=True)
    if user is None or user.disabled_at is not None:
        return None
    has_totp = (
        await db.execute(
            select(mfa_totp.c.id).where(
                mfa_totp.c.user_id == approver, mfa_totp.c.confirmed_at.is_not(None)
            )
        )
    ).first()
    if has_totp is None:
        return None
    row = (
        await db.execute(
            update(qr_logins)
            .where(
                *bound,
                qr_logins.c.state == "approved",
                qr_logins.c.approver_user_id == approver,
                qr_logins.c.approver_session_id.is_not(None),
                qr_logins.c.complete_deadline > func.now(),
                select(sessions.c.id)
                .where(
                    sessions.c.id == qr_logins.c.approver_session_id,
                    sessions.c.user_id == approver,
                )
                .exists(),
            )
            .values(state="completed")
            .returning(
                qr_logins.c.approver_session_id,
                qr_logins.c.approved_at,
                qr_logins.c.approved_factor,
                qr_logins.c.desktop_ip,
                qr_logins.c.desktop_browser,
                qr_logins.c.desktop_os,
                qr_logins.c.desktop_place,
            )
        )
    ).first()
    if row is None:
        return None
    issued = await start_approved_session(
        db,
        settings,
        approver,
        approved_at=row.approved_at,
        factor=row.approved_factor,
        approved_by=row.approver_session_id,
        user_agent=user_agent,
        ip=ip,
        replacing_token=replacing_token,
    )
    role = (await db.execute(select(users.c.role).where(users.c.id == approver))).scalar_one()
    desktop = _desktop(row)
    await record_event(
        db,
        "qr_login_completed",
        user_id=approver,
        ip=ip,
        detail={"qr": str(row_id), "session": str(issued.id), "place": desktop.place},
    )
    await _notify(db, row_id)
    return Completed(issued, approver, user.email, role, desktop)


# --- the phone ----------------------------------------------------------------------------------


async def scan(db: AsyncSession, nonce: bytes, session: SessionInfo) -> Scanned | None:
    """The first scan wins: the row is taken for this session if it is pending, within its 60
    seconds and still bound to a pre-session. ``None`` otherwise (the generic answer)."""
    row = (
        await db.execute(
            update(qr_logins)
            .where(
                qr_logins.c.nonce_hash == nonce_hash(nonce),
                qr_logins.c.state == "pending",
                qr_logins.c.scan_deadline > func.now(),
                qr_logins.c.presession_id.is_not(None),
            )
            .values(
                state="scanned",
                approver_user_id=session.user_id,
                approver_session_id=session.id,
                scanned_at=func.now(),
                approve_deadline=func.now() + APPROVE_WINDOW,
            )
            .returning(
                qr_logins.c.id,
                qr_logins.c.created_at,
                qr_logins.c.approve_deadline,
                qr_logins.c.desktop_ip,
                qr_logins.c.desktop_browser,
                qr_logins.c.desktop_os,
                qr_logins.c.desktop_place,
                func.now().label("now"),
            )
        )
    ).first()
    if row is None:
        return None
    await _notify(db, row.id)
    return Scanned(row.id, _desktop(row), row.created_at, row.approve_deadline, row.now)


def _approvable(row_id: UUID, session: SessionInfo) -> tuple[Any, ...]:
    return (
        qr_logins.c.id == row_id,
        qr_logins.c.state == "scanned",
        qr_logins.c.approver_user_id == session.user_id,
        qr_logins.c.approver_session_id == session.id,
        qr_logins.c.approve_deadline > func.now(),
    )


async def approve(
    db: AsyncSession,
    keys: Keys,
    ring: KeyRing,
    row_id: UUID,
    session: SessionInfo,
    code: str,
    ip: str | None,
) -> Approval:
    """Approve with a TOTP or recovery code entered for this approval (section 5, step 4): the
    step-up verifier (a TOTP step once, a recovery code once, failures as ``mfa_failed``), but
    an earlier step-up does not count and the phone's ``stepup_at`` is not set.

    Order: the account row (``FOR SHARE``), then the row's state (no code is checked for a row
    that can't be approved), then the code, then the conditional update. A code that passed for
    a row that was taken meanwhile stays used."""
    user = await lock_user(db, session.user_id, share=True)
    if user is None or user.disabled_at is not None:
        return Approval("unavailable")
    if (
        await db.execute(select(qr_logins.c.id).where(*_approvable(row_id, session)))
    ).first() is None:
        return Approval("unavailable")
    try:
        method = await mfa.check_second_factor(db, keys, ring, session.user_id, code)
    except TotpUnreadable as unreadable:
        # Fails closed with the generic error; not an mfa_failed (ADR 0004 section 2).
        await record_event(db, unreadable.kind, user_id=session.user_id, ip=ip)
        return Approval("invalid_code")
    if method is None:
        mail = await mfa.record_failure(db, session.user_id, ip, "qr_approve")
        return Approval("invalid_code", mail_failures=mail)
    factor = FACTORS[method]
    row = (
        await db.execute(
            update(qr_logins)
            .where(*_approvable(row_id, session))
            .values(
                state="approved",
                approved_at=func.now(),
                approved_factor=factor,
                complete_deadline=func.now() + COMPLETE_WINDOW,
            )
            .returning(qr_logins.c.desktop_place)
        )
    ).first()
    if row is None:
        return Approval("unavailable")
    await record_event(
        db,
        "qr_login_approved",
        user_id=session.user_id,
        ip=ip,
        detail={"qr": str(row_id), "factor": factor, "place": row.desktop_place},
    )
    await _notify(db, row_id)
    return Approval("approved")


async def deny(db: AsyncSession, row_id: UUID, session: SessionInfo, ip: str | None) -> bool:
    """End a scanned request; the desktop's stream says ``denied`` and closes."""
    row = (
        await db.execute(
            update(qr_logins)
            .where(*_approvable(row_id, session))
            .values(state="denied")
            .returning(qr_logins.c.id)
        )
    ).first()
    if row is None:
        return False
    await record_event(
        db, "qr_login_denied", user_id=session.user_id, ip=ip, detail={"qr": str(row_id)}
    )
    await _notify(db, row_id)
    return True


def last_deadline() -> Any:
    """The row's last deadline, for the purge: the latest one it reached."""
    return func.coalesce(
        qr_logins.c.complete_deadline, qr_logins.c.approve_deadline, qr_logins.c.scan_deadline
    )
