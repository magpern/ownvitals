"""``oauth_flows`` and ``identities`` (ADR 0003 sections 1 and 5).

A flow is created at the start and taken exactly once at the callback with
``DELETE ... RETURNING``, so a replayed or parallel callback finds nothing. The flow id, like the
state, is stored only as a SHA-256. Every flow has its own nonce and PKCE ``code_verifier``
(RFC 7636, 64 characters), stored with the state and read back only by ``take_flow``; the
callback sends the verifier with the code exchange (``oauth_client.identify``).
"""

import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.keys import new_token, token_hash
from ownvitals.auth.tables import identities, oauth_flows, users

FLOW_LIFETIME = timedelta(minutes=10)
RETURN_TO_MAX = 512
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def safe_return_to(value: str | None) -> str | None:
    """Only a relative path with a single leading ``/`` (T36): no scheme, host or backslash."""
    if (
        not value
        or len(value) > RETURN_TO_MAX
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or _CONTROL.search(value)
        or "://" in value
    ):
        return None
    return value


@dataclass(frozen=True, slots=True)
class NewFlow:
    cookie: str  # the flow id for the __Host-ownvitals_oauth cookie
    state: str
    nonce: str
    verifier: str


@dataclass(frozen=True, slots=True)
class Flow:
    provider: str
    purpose: str
    state_hash: bytes
    nonce: str
    verifier: str
    user_id: UUID | None
    invite_id: UUID | None
    return_to: str | None

    def state_matches(self, state: str) -> bool:
        return hmac.compare_digest(self.state_hash, token_hash(state))


async def create_flow(
    db: AsyncSession,
    *,
    provider: str,
    purpose: str,
    user_id: UUID | None,
    invite_id: UUID | None,
    return_to: str | None,
) -> NewFlow:
    flow = NewFlow(
        cookie=new_token(),
        state=new_token(),
        nonce=new_token(),
        verifier=secrets.token_urlsafe(48),  # 64 characters: within RFC 7636's 43 to 128
    )
    await db.execute(
        insert(oauth_flows).values(
            flow_hash=token_hash(flow.cookie),
            provider=provider,
            purpose=purpose,
            state_hash=token_hash(flow.state),
            nonce=flow.nonce,
            pkce_verifier=flow.verifier,
            user_id=user_id,
            invite_id=invite_id,
            return_to=return_to,
            expires_at=func.now() + FLOW_LIFETIME,
        )
    )
    return flow


async def take_flow(db: AsyncSession, cookie: str) -> Flow | None:
    """Atomic single use: the row is deleted by the request that reads it."""
    row = (
        await db.execute(
            delete(oauth_flows)
            .where(
                oauth_flows.c.flow_hash == token_hash(cookie),
                oauth_flows.c.expires_at > func.now(),
            )
            .returning(
                oauth_flows.c.provider,
                oauth_flows.c.purpose,
                oauth_flows.c.state_hash,
                oauth_flows.c.nonce,
                oauth_flows.c.pkce_verifier,
                oauth_flows.c.user_id,
                oauth_flows.c.invite_id,
                oauth_flows.c.return_to,
            )
        )
    ).first()
    return None if row is None else Flow(*row)


@dataclass(frozen=True, slots=True)
class IdentityRow:
    id: UUID
    provider: str
    email_at_link: str | None
    created_at: datetime


async def list_identities(db: AsyncSession, user_id: UUID) -> list[IdentityRow]:
    rows = await db.execute(
        select(
            identities.c.id,
            identities.c.provider,
            identities.c.email_at_link,
            identities.c.created_at,
        )
        .where(identities.c.user_id == user_id)
        .order_by(identities.c.created_at)
    )
    return [IdentityRow(*row) for row in rows.all()]


async def identity_owner(db: AsyncSession, provider: str, subject: str) -> tuple[UUID, bool] | None:
    """``(user_id, user is disabled)`` of the user the identity belongs to."""
    row = (
        await db.execute(
            select(identities.c.user_id, users.c.disabled_at)
            .join(users, users.c.id == identities.c.user_id)
            .where(identities.c.provider == provider, identities.c.subject == subject)
        )
    ).first()
    return None if row is None else (row.user_id, row.disabled_at is not None)


async def add_identity(
    db: AsyncSession, user_id: UUID, provider: str, subject: str, email: str | None
) -> bool:
    """Link the identity. False when ``(provider, subject)`` already exists (nothing changes)."""
    try:
        async with db.begin_nested():
            await db.execute(
                insert(identities).values(
                    user_id=user_id, provider=provider, subject=subject, email_at_link=email
                )
            )
    except IntegrityError:
        return False  # never logged: its DETAIL holds the subject
    return True


async def remove_identity(db: AsyncSession, user_id: UUID, identity_id: UUID) -> str:
    """Unlink one of the user's own identities. ``"gone"`` for an unknown id and for another
    user's (the caller answers 404), ``"last"`` when it is the user's only sign-in method,
    else ``"removed"``. The user row is locked, so two parallel unlinks cannot remove both."""
    await db.execute(select(users.c.id).where(users.c.id == user_id).with_for_update())
    own = (
        (await db.execute(select(identities.c.id).where(identities.c.user_id == user_id)))
        .scalars()
        .all()
    )
    if identity_id not in own:
        return "gone"
    has_password = (
        await db.execute(select(users.c.password_hash.is_not(None)).where(users.c.id == user_id))
    ).scalar_one()
    if len(own) <= 1 and not has_password:
        return "last"
    await db.execute(
        delete(identities).where(identities.c.id == identity_id, identities.c.user_id == user_id)
    )
    return "removed"
