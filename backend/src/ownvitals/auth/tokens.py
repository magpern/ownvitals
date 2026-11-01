"""Mail tokens (reset, email change; ADR 0003 section 5).

32 random bytes, only the SHA-256 stored. A token is consumed by a conditional ``UPDATE ...
RETURNING`` so it works once, whatever the number of parallel requests.
"""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.keys import new_token, token_hash
from ownvitals.auth.tables import auth_tokens

RESET_LIFETIME = timedelta(minutes=30)
EMAIL_CHANGE_LIFETIME = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class MailToken:
    id: UUID
    user_id: UUID
    new_email: str | None
    email_at_issue: str | None


async def issue_token(
    db: AsyncSession,
    purpose: str,
    user_id: UUID,
    lifetime: timedelta,
    *,
    new_email: str | None = None,
    email_at_issue: str | None = None,
) -> str:
    token = new_token()
    await db.execute(
        insert(auth_tokens).values(
            purpose=purpose,
            token_hash=token_hash(token),
            user_id=user_id,
            new_email=new_email,
            email_at_issue=email_at_issue,
            expires_at=func.now() + lifetime,
        )
    )
    return token


_COLUMNS = (
    auth_tokens.c.id,
    auth_tokens.c.user_id,
    auth_tokens.c.new_email,
    auth_tokens.c.email_at_issue,
)


async def peek_token(db: AsyncSession, purpose: str, token: str) -> MailToken | None:
    """A valid (unused, unexpired) token of the purpose, without consuming it."""
    row = (
        await db.execute(
            select(*_COLUMNS).where(
                auth_tokens.c.token_hash == token_hash(token),
                auth_tokens.c.purpose == purpose,
                auth_tokens.c.used_at.is_(None),
                auth_tokens.c.expires_at > func.now(),
            )
        )
    ).first()
    return None if row is None else MailToken(*row)


async def consume_token(
    db: AsyncSession, purpose: str, token: str, *, user_id: UUID | None = None
) -> MailToken | None:
    """Single use: ``UPDATE ... SET used_at = now() WHERE used_at IS NULL AND expires_at > now()
    RETURNING``. ``user_id`` additionally pins the token to one user (email change)."""
    statement = (
        update(auth_tokens)
        .where(
            auth_tokens.c.token_hash == token_hash(token),
            auth_tokens.c.purpose == purpose,
            auth_tokens.c.used_at.is_(None),
            auth_tokens.c.expires_at > func.now(),
        )
        .values(used_at=func.now())
        .returning(*_COLUMNS)
    )
    if user_id is not None:
        statement = statement.where(auth_tokens.c.user_id == user_id)
    row = (await db.execute(statement)).first()
    return None if row is None else MailToken(*row)


async def delete_outstanding_tokens(db: AsyncSession, user_id: UUID) -> None:
    """A password change, a reset or an email change invalidates the user's other tokens."""
    await db.execute(delete(auth_tokens).where(auth_tokens.c.user_id == user_id))
