"""QR approve-login, the phone's side: scan, approve with a code, deny (ADR 0011 section 5).

Part of the state machine described in ``qrlogin`` (one conditional ``UPDATE`` per
transition, the account row locked first by ``approve``, a ``pg_notify`` per transition).
"""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth import mfa
from ownvitals.auth.events import record_event
from ownvitals.auth.keys import Keys
from ownvitals.auth.passkeys import lock_user
from ownvitals.auth.qrlogin import _notify
from ownvitals.auth.qrlogin_model import (
    APPROVE_WINDOW,
    COMPLETE_WINDOW,
    FACTORS,
    Approval,
    Scanned,
    _desktop,
    nonce_hash,
)
from ownvitals.auth.sessions import SessionInfo
from ownvitals.auth.tables import qr_logins
from ownvitals.auth.totp_keys import TotpUnreadable
from ownvitals.crypto.keks import KeyRing


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
