"""Passkeys: the stored credentials a ceremony reads, and the ceremony's input types (ADR 0011
section 1).

The account's user handle, the credential lookup with its account's state, and what an
authentication response must carry. No WebAuthn library: ``passkey_ceremonies`` holds the
ceremonies and every check, and re-exports these names.
"""

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.passkeys import HANDLE_BYTES
from ownvitals.auth.tables import mfa_totp, users, webauthn_credentials


async def _user_handle(db: AsyncSession, user_id: UUID) -> bytes:
    """The account's WebAuthn user handle, created on first use (32 random bytes)."""
    handle = (
        await db.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(
                webauthn_handle=func.coalesce(
                    users.c.webauthn_handle, secrets.token_bytes(HANDLE_BYTES)
                )
            )
            .returning(users.c.webauthn_handle)
        )
    ).scalar_one()
    return bytes(handle)


@dataclass(frozen=True, slots=True)
class StoredCredential:
    id: UUID
    user_id: UUID
    email: str
    email_normalized: str
    name: str
    public_key: bytes
    sign_count: int
    suspended: bool
    disabled: bool
    handle: bytes | None
    totp_confirmed: bool


def _totp_confirmed() -> Any:
    return (
        select(mfa_totp.c.id)
        .where(mfa_totp.c.user_id == users.c.id, mfa_totp.c.confirmed_at.is_not(None))
        .exists()
    )


async def find_credential(db: AsyncSession, credential_id: bytes) -> StoredCredential | None:
    row = (
        await db.execute(
            select(
                webauthn_credentials.c.id,
                webauthn_credentials.c.user_id,
                users.c.email,
                users.c.email_normalized,
                webauthn_credentials.c.name,
                webauthn_credentials.c.public_key,
                webauthn_credentials.c.sign_count,
                webauthn_credentials.c.suspended_at.is_not(None).label("suspended"),
                users.c.disabled_at.is_not(None).label("disabled"),
                users.c.webauthn_handle,
                _totp_confirmed().label("totp_confirmed"),
            )
            .join(users, users.c.id == webauthn_credentials.c.user_id)
            .where(webauthn_credentials.c.credential_id == credential_id)
        )
    ).first()
    if row is None:
        return None
    return StoredCredential(
        row.id,
        row.user_id,
        row.email,
        row.email_normalized,
        row.name,
        bytes(row.public_key),
        int(row.sign_count),
        row.suspended,
        row.disabled,
        None if row.webauthn_handle is None else bytes(row.webauthn_handle),
        row.totp_confirmed,
    )


@dataclass(frozen=True, slots=True)
class Assertion:
    """The parts of an authentication response the checks before the signature need."""

    credential: dict[str, Any]  # as the browser sent it, for py_webauthn
    credential_id: bytes
    client_data: str
    user_handle: bytes | None


Gate = Callable[[StoredCredential | None], Awaitable[None]]
