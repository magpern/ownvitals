"""TOTP seeds on the envelope scheme of ADR 0004 (ADR 0003 section 5).

Every ``mfa_totp`` row is in one form, from the first enrolment: the seed sealed under a
per-write data key, payload AAD ``ownvitals-totp-v1|<user_id>`` (``envelope.totp_aad``); the data
key wrapped under a KEK, AAD ``ownvitals-totp-dek-v1|<key_id>|<user_id>``
(``envelope.totp_wrap_aad``). A check constraint on the table pins the form (``key_id`` a KEK id,
a 48-byte wrapped key and a 12-byte nonce). classbeat's interim ``app-v1`` form and its
``keys rewrap --totp`` migration are not ported: no seed was ever written in it here.

A row that does not decrypt fails MFA closed with the generic error and does not count toward
``mfa_failed``: ``TotpUnreadable`` carries the auth event to write (``totp_key_unavailable``, the
operator restores the KEK file; ``totp_decrypt_failed``, fixed by ``admin mfa-reset``).

``rewrap_totp_secrets`` is what ``keys rewrap`` (#13) runs for the seeds: only the wrap changes.
"""

import logging
from collections import Counter
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.tables import mfa_totp
from ownvitals.crypto.envelope import (
    DecryptFailed,
    KeyRetired,
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
    """The column values for a new or replaced secret (a new data key every write), under the
    current KEK."""
    kek = await ring.current(db)
    sealed = seal(secret, totp_aad(user_id), kek, totp_wrap_aad(kek.key_id, user_id))
    return {
        "secret_ciphertext": sealed.ciphertext,
        "nonce": sealed.nonce,
        "key_id": sealed.key_id,
        "wrapped_dek": sealed.wrapped_dek,
        "dek_nonce": sealed.dek_nonce,
    }


async def open_totp_secret(db: AsyncSession, ring: KeyRing, user_id: UUID, row: Any) -> bytes:
    """Decrypt a row (``secret_ciphertext``, ``nonce``, ``key_id``, ``wrapped_dek``,
    ``dek_nonce``) of ``user_id``, or ``TotpUnreadable``. Only the KEK the row names is tried,
    and both AADs name ``user_id``: another user's row never opens."""
    kind: Unreadable | None = None
    secret: bytes | None = None
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
    """How many TOTP rows each KEK id holds."""
    async with system_session(factory) as db:
        rows = await db.execute(select(_T.key_id, func.count()).group_by(_T.key_id))
        return Counter({row[0]: row[1] for row in rows})


async def rewrap_totp_secrets(
    factory: async_sessionmaker[AsyncSession], ring: KeyRing
) -> Counter[str]:
    """Move every row not under the current KEK onto it (only the wrap changes), one row per
    transaction, ``UPDATE ... WHERE user_id = :u AND key_id = :old``: a seed the user replaced
    meanwhile is never overwritten. Counts per ``<old key id> <outcome>``."""
    condition = _T.key_id != ring.current_id
    async with system_session(factory) as db:
        pending = list((await db.execute(select(_T.user_id).where(condition))).scalars().all())
    counts: Counter[str] = Counter()
    for user_id in pending:
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
            row.wrapped_dek,
            row.dek_nonce,
            old,
            totp_wrap_aad(old.key_id, user_id),
            current,
            totp_wrap_aad(current.key_id, user_id),
        )
    except (DecryptFailed, KeyRetired):
        return "decrypt_failed"
    result = await db.execute(
        update(mfa_totp)
        .where(_T.user_id == user_id, _T.key_id == row.key_id)
        .values(wrapped_dek=wrapped, dek_nonce=nonce, key_id=current.key_id)
        .returning(_T.id)
    )
    return "rewrapped" if result.first() is not None else "skipped"
