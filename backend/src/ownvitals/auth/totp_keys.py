"""TOTP secrets on the envelope primitive (ADR 0004 §2, "TOTP").

Two forms of ``mfa_totp`` row, told apart by ``key_id`` and a check constraint:

- ``app-v1`` (ADR 0003 §8): AES-256-GCM under the HKDF key ``totp-v1``, AAD ``<user_id>|totp``,
  no wrapped key. Still decrypts until ``keys rewrap --totp`` migrates it.
- v2 (``key_id`` a KEK id): the secret under a per-write data key, AAD
  ``ownvitals-totp-v2|<user_id>``; the data key wrapped under the KEK, AAD
  ``ownvitals-totp-dek-v1|<key_id>|<user_id>``. Every new enrollment writes v2.

A row that does not decrypt fails MFA closed with the generic error and does not count toward
``mfa_failed``: ``TotpUnreadable`` carries the auth event to write (``totp_key_unavailable``, the
operator restores the KEK file; ``totp_decrypt_failed``, fixed by ``admin mfa-reset``).
"""

import logging
from collections import Counter
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.keys import TOTP_KEY_ID, Keys, decrypt_totp_secret
from ownvitals.auth.tables import mfa_totp
from ownvitals.crypto.envelope import (
    DecryptFailed,
    open_sealed,
    rewrap,
    seal,
    totp_aad,
    totp_wrap_aad,
)
from ownvitals.crypto.keks import KeyRing, KeyUnavailable
from ownvitals.db import system_session

logger = logging.getLogger(__name__)

Unreadable = Literal["totp_key_unavailable", "totp_decrypt_failed"]
_T = mfa_totp.c


class TotpUnreadable(Exception):
    """The stored secret cannot be read; ``kind`` is the auth event to write."""

    def __init__(self, kind: Unreadable) -> None:
        super().__init__(kind)
        self.kind: Unreadable = kind


async def seal_totp_secret(
    db: AsyncSession, ring: KeyRing, user_id: UUID, secret: bytes
) -> dict[str, Any]:
    """The v2 column values for a new or replaced secret (a new data key every write)."""
    kek = await ring.current(db)
    sealed = seal(secret, totp_aad(user_id), kek, totp_wrap_aad(kek.key_id, user_id))
    return {
        "secret_ciphertext": sealed.ciphertext,
        "nonce": sealed.nonce,
        "key_id": sealed.key_id,
        "wrapped_dek": sealed.wrapped_dek,
        "dek_nonce": sealed.dek_nonce,
    }


async def open_totp_secret(
    db: AsyncSession, keys: Keys, ring: KeyRing, user_id: UUID, row: Any
) -> bytes:
    """Decrypt a row (``secret_ciphertext``, ``nonce``, ``key_id``, ``wrapped_dek``,
    ``dek_nonce``) of ``user_id``, or ``TotpUnreadable``. No other key is ever tried."""
    kind: Unreadable | None = None
    secret: bytes | None = None
    if row.key_id == TOTP_KEY_ID:
        try:
            secret = decrypt_totp_secret(
                keys, user_id, bytes(row.secret_ciphertext), bytes(row.nonce), row.key_id
            )
        except ValueError:
            kind = "totp_decrypt_failed"
    else:
        try:
            kek = await ring.kek_for(db, row.key_id)
        except KeyUnavailable:
            kek = None
            kind = "totp_key_unavailable"
        if kek is not None:
            try:
                secret = open_sealed(
                    row.secret_ciphertext,
                    row.nonce,
                    row.wrapped_dek,
                    row.dek_nonce,
                    kek,
                    totp_aad(user_id),
                    totp_wrap_aad(kek.key_id, user_id),
                )
            except DecryptFailed:
                kind = "totp_decrypt_failed"
    if secret is None:
        kind = kind or "totp_decrypt_failed"
        logger.error("a stored TOTP secret cannot be read", extra={"category": kind})
        raise TotpUnreadable(kind)
    return secret


_COLUMNS = (_T.user_id, _T.secret_ciphertext, _T.nonce, _T.key_id, _T.wrapped_dek, _T.dek_nonce)


async def totp_rows_by_key(factory: async_sessionmaker[AsyncSession]) -> Counter[str]:
    """How many TOTP rows each ``key_id`` (``app-v1`` or a KEK id) holds."""
    async with system_session(factory) as db:
        rows = await db.execute(select(_T.key_id, func.count()).group_by(_T.key_id))
        return Counter({row[0]: row[1] for row in rows})


async def _pending(factory: async_sessionmaker[AsyncSession], condition: Any) -> list[UUID]:
    async with system_session(factory) as db:
        return list((await db.execute(select(_T.user_id).where(condition))).scalars().all())


async def migrate_v1_secrets(
    factory: async_sessionmaker[AsyncSession], keys: Keys, ring: KeyRing
) -> Counter[str]:
    """``keys rewrap --totp``: re-encrypt every ``app-v1`` row as v2, one row per transaction.
    The ``UPDATE`` is conditional on ``key_id = 'app-v1'``, so a secret the user replaced
    meanwhile is never overwritten."""
    counts: Counter[str] = Counter()
    for user_id in await _pending(factory, _T.key_id == TOTP_KEY_ID):
        async with system_session(factory) as db:
            row = (
                await db.execute(
                    select(*_COLUMNS)
                    .where(_T.user_id == user_id, _T.key_id == TOTP_KEY_ID)
                    .with_for_update()
                )
            ).first()
            if row is None:
                counts[f"{TOTP_KEY_ID} skipped"] += 1
                continue
            try:
                secret = await open_totp_secret(db, keys, ring, user_id, row)
            except TotpUnreadable:
                counts[f"{TOTP_KEY_ID} decrypt_failed"] += 1
                continue
            result = await db.execute(
                update(mfa_totp)
                .where(_T.user_id == user_id, _T.key_id == TOTP_KEY_ID)
                .values(**(await seal_totp_secret(db, ring, user_id, secret)))
                .returning(_T.id)
            )
            outcome = "migrated" if result.first() is not None else "skipped"
            counts[f"{TOTP_KEY_ID} {outcome}"] += 1
    return counts


async def rewrap_v2_secrets(
    factory: async_sessionmaker[AsyncSession], ring: KeyRing
) -> Counter[str]:
    """``keys rewrap``: move every v2 row not under the current KEK onto it (only the wrap
    changes), one row per transaction, ``UPDATE ... WHERE user_id = :u AND key_id = :old``."""
    condition = (_T.key_id != TOTP_KEY_ID) & (_T.key_id != ring.current_id)
    counts: Counter[str] = Counter()
    for user_id in await _pending(factory, condition):
        async with system_session(factory) as db:
            row = (
                await db.execute(
                    select(*_COLUMNS).where(_T.user_id == user_id, condition).with_for_update()
                )
            ).first()
            if row is None:
                counts["- skipped"] += 1
                continue
            outcome = await _rewrap_row(db, ring, user_id, row)
            counts[f"{row.key_id} {outcome}"] += 1
    return counts


async def _rewrap_row(db: AsyncSession, ring: KeyRing, user_id: UUID, row: Any) -> str:
    try:
        old = await ring.kek_for(db, row.key_id)
    except KeyUnavailable:
        return "key_unavailable"
    current = await ring.current(db)
    try:
        wrapped, nonce = rewrap(
            bytes(row.wrapped_dek),
            bytes(row.dek_nonce),
            old,
            totp_wrap_aad(old.key_id, user_id),
            current,
            totp_wrap_aad(current.key_id, user_id),
        )
    except DecryptFailed:
        return "decrypt_failed"
    result = await db.execute(
        update(mfa_totp)
        .where(_T.user_id == user_id, _T.key_id == row.key_id)
        .values(wrapped_dek=wrapped, dek_nonce=nonce, key_id=current.key_id)
        .returning(_T.id)
    )
    return "rewrapped" if result.first() is not None else "skipped"
