"""The pre-session (ADR 0011 section 4).

A browser without a session gets one pre-session cookie, ``__Host-ownvitals_pre``, when it
starts a passkey sign-in (and, with the QR issue, a QR login). It grants nothing: it only binds a
challenge (or a QR row) to the browser that asked for it. The cookie holds 32 random bytes; the
database holds their SHA-256 and an expiry 5 minutes later. Every start issues a new one and
deletes the old one, which takes what it bound with it (``ON DELETE CASCADE``). It is read only
from its cookie, never from a header, a query or a body, and never logged.
"""

from datetime import timedelta
from uuid import UUID

from fastapi import Request
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.cookies import PRE_COOKIE
from ownvitals.auth.keys import new_token, token_hash
from ownvitals.auth.tables import pre_sessions

LIFETIME = timedelta(minutes=5)
TOKEN_MAX = 128  # a token_urlsafe(32) is 43 characters; anything far longer is junk


def request_pre_token(request: Request) -> str | None:
    """The pre-session token from its cookie only."""
    value = request.cookies.get(PRE_COOKIE)
    if not value or len(value) > TOKEN_MAX:
        return None
    return value


async def issue(db: AsyncSession, replacing: str | None) -> tuple[UUID, str]:
    """A new pre-session; the browser's old one (if any) is deleted with what it bound."""
    if replacing:
        await db.execute(
            delete(pre_sessions).where(pre_sessions.c.token_hash == token_hash(replacing))
        )
    token = new_token()
    row_id = (
        await db.execute(
            insert(pre_sessions)
            .values(token_hash=token_hash(token), expires_at=func.now() + LIFETIME)
            .returning(pre_sessions.c.id)
        )
    ).scalar_one()
    return row_id, token


async def find(db: AsyncSession, token: str | None) -> UUID | None:
    """The live pre-session of ``token``, or ``None`` (none, unknown or expired)."""
    if not token:
        return None
    return (
        await db.execute(
            select(pre_sessions.c.id).where(
                pre_sessions.c.token_hash == token_hash(token),
                pre_sessions.c.expires_at > func.now(),
            )
        )
    ).scalar_one_or_none()


async def end(db: AsyncSession, token: str | None) -> None:
    """Delete the pre-session of ``token`` (after a sign-in), with what it bound."""
    if token:
        await db.execute(delete(pre_sessions).where(pre_sessions.c.token_hash == token_hash(token)))
