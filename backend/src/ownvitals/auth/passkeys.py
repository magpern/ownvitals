"""Passkeys: the shared core (ADR 0011 sections 1 to 4).

The failure type and its closed set of reasons, the relying party, the passkey rows the account
page shows, challenges (stored as SHA-256, single-use, bound to a session or a pre-session), the
account-row lock, and listing, renaming and removing passkeys. The ceremonies themselves, and the
only import of ``py_webauthn``, are in ``passkey_ceremonies``; this module needs no WebAuthn
library, so the operator CLI and the reset flow use it without loading one.

Lock order: every path that adds or uses a sign-in method locks the account row (``lock_user``)
before it re-checks the account's state, and ``accounts.reset_mfa`` locks it ``FOR UPDATE``
before it removes them (ADR 0011 section 3, the #165 race finding).

Nothing here logs. Challenges, credential ids, public keys and user handles never reach a log,
an ``auth_events`` row or an error message (section 9).
"""

import hashlib
import secrets
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# The values, the failure type and the parsing are in ``passkey_model`` (pure).
from ownvitals.auth.passkey_model import (  # noqa: F401 - re-exported
    CHALLENGE_BYTES,
    CHALLENGE_LIFETIME,
    COUNTER_MAX,
    CREDENTIAL_ID_MAX,
    DEFAULT_NAME,
    HANDLE_BYTES,
    NAME_MAX,
    REASONS,
    TIMEOUT_MS,
    Passkey,
    PasskeyFailure,
    Purpose,
    RelyingParty,
    b64url_decode,
    normalize_name,
    read_client_data,
)
from ownvitals.auth.tables import users, webauthn_challenges, webauthn_credentials
from ownvitals.config import Settings
from ownvitals.db import system_session


def relying_party(settings: Settings) -> RelyingParty | None:
    """``None`` when the public host is an IP address (passkeys are off; ``config``)."""
    rp_id = settings.passkey_rp_id
    return None if rp_id is None else RelyingParty(rp_id, settings.public_base_url)


PASSKEY_COLUMNS = (
    webauthn_credentials.c.id,
    webauthn_credentials.c.name,
    webauthn_credentials.c.created_at,
    webauthn_credentials.c.last_used_at,
    webauthn_credentials.c.backup_eligible,
    webauthn_credentials.c.backup_state,
    webauthn_credentials.c.suspended_at.is_not(None).label("suspended"),
)


def to_passkey(row: Any) -> Passkey:
    return Passkey(
        row.id,
        row.name,
        row.created_at,
        row.last_used_at,
        row.backup_eligible,
        row.backup_state,
        row.suspended,
    )


def _challenge_hash(challenge: bytes) -> bytes:
    return hashlib.sha256(challenge).digest()


# --- challenges ---------------------------------------------------------------------------------


async def new_challenge(
    db: AsyncSession,
    purpose: Purpose,
    *,
    user_id: UUID | None = None,
    session_id: UUID | None = None,
    presession_id: UUID | None = None,
) -> bytes:
    """A fresh challenge bound to a session (``register``, ``reauth``) or a pre-session
    (``signin``). At most one live challenge per purpose and binding: a new one replaces it."""
    if purpose == "signin":
        binding = webauthn_challenges.c.presession_id == presession_id
    else:
        binding = webauthn_challenges.c.session_id == session_id
    await db.execute(
        delete(webauthn_challenges).where(webauthn_challenges.c.purpose == purpose, binding)
    )
    challenge = secrets.token_bytes(CHALLENGE_BYTES)
    await db.execute(
        insert(webauthn_challenges).values(
            challenge_hash=_challenge_hash(challenge),
            purpose=purpose,
            user_id=user_id,
            session_id=session_id,
            presession_id=presession_id,
            expires_at=func.now() + CHALLENGE_LIFETIME,
        )
    )
    return challenge


async def take_challenge(
    factory: async_sessionmaker[AsyncSession],
    challenge: bytes,
    purpose: Purpose,
    *,
    session_id: UUID | None = None,
    user_id: UUID | None = None,
    presession_id: UUID | None = None,
) -> None:
    """Delete the challenge (committed at once, whatever happens next) and check that it is
    live, of this purpose and bound to the caller. ``PasskeyFailure("challenge")`` otherwise."""
    async with system_session(factory) as db:
        row = (
            await db.execute(
                delete(webauthn_challenges)
                .where(webauthn_challenges.c.challenge_hash == _challenge_hash(challenge))
                .returning(
                    webauthn_challenges.c.purpose,
                    webauthn_challenges.c.user_id,
                    webauthn_challenges.c.session_id,
                    webauthn_challenges.c.presession_id,
                    (webauthn_challenges.c.expires_at > func.now()).label("live"),
                )
            )
        ).first()
    if (
        row is None
        or not row.live
        or row.purpose != purpose
        or row.session_id != session_id
        or row.user_id != user_id
        or row.presession_id != presession_id
        or (purpose == "signin" and presession_id is None)
        or (purpose != "signin" and (session_id is None or user_id is None))
    ):
        raise PasskeyFailure("challenge", user_id=user_id)


# --- the account row ----------------------------------------------------------------------------


async def lock_user(db: AsyncSession, user_id: UUID, *, share: bool = False) -> Any:
    """Lock the account row (``FOR SHARE`` or ``FOR UPDATE``); returns it or ``None``. Every
    path that adds or uses a sign-in method locks it before it re-checks its state, and
    ``reset_mfa`` locks it ``FOR UPDATE`` before it removes them."""
    statement = select(users.c.id, users.c.email, users.c.disabled_at).where(users.c.id == user_id)
    statement = statement.with_for_update(read=share)
    return (await db.execute(statement)).first()


# --- management ---------------------------------------------------------------------------------


async def list_passkeys(db: AsyncSession, user_id: UUID) -> list[Passkey]:
    rows = await db.execute(
        select(*PASSKEY_COLUMNS)
        .where(webauthn_credentials.c.user_id == user_id)
        .order_by(webauthn_credentials.c.created_at, webauthn_credentials.c.id)
    )
    return [to_passkey(row) for row in rows.all()]


async def rename_passkey(
    db: AsyncSession, user_id: UUID, passkey_id: UUID, name: str
) -> Passkey | None:
    """``None`` for an unknown id and for another user's (the caller answers 404)."""
    row = (
        await db.execute(
            update(webauthn_credentials)
            .where(
                webauthn_credentials.c.id == passkey_id,
                webauthn_credentials.c.user_id == user_id,
            )
            .values(name=name)
            .returning(*PASSKEY_COLUMNS)
        )
    ).first()
    return None if row is None else to_passkey(row)


async def remove_passkey(db: AsyncSession, user_id: UUID, passkey_id: UUID) -> str | None:
    """The removed passkey's name, or ``None`` for an unknown id and another user's."""
    return (
        await db.execute(
            delete(webauthn_credentials)
            .where(
                webauthn_credentials.c.id == passkey_id,
                webauthn_credentials.c.user_id == user_id,
            )
            .returning(webauthn_credentials.c.name)
        )
    ).scalar_one_or_none()


async def remove_all_passkeys(db: AsyncSession, user_id: UUID) -> int:
    """Account recovery (section 3): every passkey of the account, and its open challenges."""
    await db.execute(delete(webauthn_challenges).where(webauthn_challenges.c.user_id == user_id))
    result = await db.execute(
        delete(webauthn_credentials)
        .where(webauthn_credentials.c.user_id == user_id)
        .returning(webauthn_credentials.c.id)
    )
    return len(result.all())


async def passkeys_for_mail(db: AsyncSession, user_id: UUID) -> list[tuple[str, datetime]]:
    """Names and creation times, for the password-reset mail (section 3)."""
    return [(p.name, p.created_at) for p in await list_passkeys(db, user_id)]
