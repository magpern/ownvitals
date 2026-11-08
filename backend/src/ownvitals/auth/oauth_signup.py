"""The account a provider sign-up creates (ADR 0003 section 5): the invite check, its
consumption, and the user with its identity in one savepoint. Moved out of ``oauth_outcomes``
unchanged (#248); ``oauth_outcomes`` re-exports the names ``oauth_handoff`` imports.
"""

from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.accounts import create_user, use_invite
from ownvitals.auth.locale import still_offered
from ownvitals.auth.tables import identities, invites
from ownvitals.config import Settings


class Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def invite_valid(db: AsyncSession, invite_id: UUID | None) -> bool:
    if invite_id is None:
        return False
    found = await db.execute(
        select(invites.c.id).where(
            invites.c.id == invite_id,
            invites.c.used_at.is_(None),
            invites.c.expires_at > func.now(),
        )
    )
    return found.first() is not None


async def consume_invite(db: AsyncSession, invite_id: UUID | None) -> None:
    """The conditional ``UPDATE`` of a provider sign-up. An invite the flow or hand-off carried
    is consumed whatever the sign-up mode is by then, so it cannot be used again. Raises
    ``Refused('invite_invalid')`` when the invite is gone; the caller's savepoint rolls
    everything back."""
    if invite_id is None:
        return
    if not await use_invite(db, invite_id):
        raise Refused("invite_invalid")


async def create_provider_account(
    db: AsyncSession,
    settings: Settings,
    *,
    invite_id: UUID | None,
    email: str,
    provider: str,
    subject: str,
    locale: str | None = None,
) -> UUID:
    """One savepoint: consume the invite, create the user (verified, with the sign-up's
    ``locale`` if it is still offered) and the identity.
    Raises ``Refused`` with ``invite_invalid`` or ``account_exists``; nothing is left behind."""
    try:
        async with db.begin_nested():
            await consume_invite(db, invite_id)
            user_id = await create_user(db, email, locale=still_offered(settings, locale))
            await db.execute(
                insert(identities).values(
                    user_id=user_id, provider=provider, subject=subject, email_at_link=email
                )
            )
            if invite_id is not None:
                await db.execute(
                    update(invites).where(invites.c.id == invite_id).values(used_by=user_id)
                )
    except IntegrityError:
        raise Refused("account_exists") from None  # never chained: DETAIL holds the address
    return user_id
