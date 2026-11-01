"""TOTP enrollment, verification and recovery codes (ADR 0003 sections 6 to 8).

All functions take the caller's ``AsyncSession`` (a ``system_session``) and filter on the
``user_id`` of the request's own session. Replay protection and recovery-code single use are
conditional ``UPDATE`` statements, so concurrent submissions of one code let exactly one win.

Secrets are stored only in ADR 0004's envelope form (``totp_keys``; ADR 0003 section 5).

A pending enrollment is not stored: ``seal_pending`` returns the new secret encrypted
(AES-256-GCM, AAD ``user_id|totp-pending``, 10 minutes) for the client to send back with the
first code. So replacing a confirmed TOTP never leaves the account half-enrolled, and a
``mfa_totp`` row is always a confirmed one.
"""

import base64
import logging
import secrets
import time
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Text, cast, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth import totp
from ownvitals.auth.keys import NONCE_LENGTH, Keys, hmac_digest
from ownvitals.auth.tables import auth_events, mfa_recovery_codes, mfa_totp
from ownvitals.auth.totp_keys import open_totp_secret, seal_totp_secret
from ownvitals.crypto.keks import KeyRing
from ownvitals.db.queue_locks import MFA_FAILURES

logger = logging.getLogger(__name__)

PENDING_LIFETIME = 600  # seconds
FAILURE_MAIL_THRESHOLD = 5  # failures in an hour

Method = Literal["totp", "recovery"]


def _pending_aad(user_id: UUID) -> bytes:
    return f"{user_id}|totp-pending".encode()


NO_TOTP = "-"  # the generation of an account without a confirmed TOTP


class EnrollmentConflict(Exception):
    """The account's TOTP changed since ``start`` (another enrollment won, or it was replaced)."""


def seal_pending(keys: Keys, user_id: UUID, secret: str, generation: str) -> str:
    """The not-yet-confirmed secret, encrypted (own HKDF key ``totp-pending``) for the client to
    send back with the first code. ``generation`` ties it to the TOTP the account had at
    ``start``, so a blob cannot be replayed after the TOTP changed."""
    nonce = secrets.token_bytes(NONCE_LENGTH)
    plain = f"{int(time.time()) + PENDING_LIFETIME}|{generation}|{secret}".encode()
    blob = AESGCM(keys.totp_pending).encrypt(nonce, plain, _pending_aad(user_id))
    return base64.urlsafe_b64encode(nonce + blob).decode()


def open_pending(keys: Keys, user_id: UUID, sealed: str) -> tuple[str, str] | None:
    """``(secret, generation)``, or ``None`` when the blob is not this user's, was altered or
    has expired."""
    try:
        raw = base64.urlsafe_b64decode(sealed.encode())
        plain = AESGCM(keys.totp_pending).decrypt(
            raw[:NONCE_LENGTH], raw[NONCE_LENGTH:], _pending_aad(user_id)
        )
        expires, generation, secret = plain.decode().split("|", 2)
        return (secret, generation) if int(expires) > time.time() else None
    except (InvalidTag, ValueError):
        return None


async def current_generation(db: AsyncSession, user_id: UUID) -> str:
    """Identifies the confirmed TOTP (its nonce changes with every replacement)."""
    nonce = (
        await db.execute(
            select(mfa_totp.c.nonce).where(
                mfa_totp.c.user_id == user_id, mfa_totp.c.confirmed_at.is_not(None)
            )
        )
    ).scalar_one_or_none()
    return bytes(nonce).hex() if nonce is not None else NO_TOTP


async def recovery_codes_left(db: AsyncSession, user_id: UUID) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(mfa_recovery_codes)
            .where(mfa_recovery_codes.c.user_id == user_id, mfa_recovery_codes.c.used_at.is_(None))
        )
    ).scalar_one()


def _recovery_hash(keys: Keys, user_id: UUID, code: str) -> bytes:
    return hmac_digest(keys.recovery_codes, str(user_id), totp.normalize_recovery_code(code))


async def regenerate_recovery_codes(
    db: AsyncSession, keys: Keys, user_id: UUID
) -> list[str] | None:
    """Ten new codes; the old ones stop working. Shown once, stored as an HMAC. The TOTP row
    is locked first, so concurrent regenerations leave exactly ten. ``None`` without a TOTP."""
    locked = await db.execute(
        select(mfa_totp.c.id).where(mfa_totp.c.user_id == user_id).with_for_update()
    )
    if locked.first() is None:
        return None
    codes = totp.new_recovery_codes()
    await db.execute(delete(mfa_recovery_codes).where(mfa_recovery_codes.c.user_id == user_id))
    await db.execute(
        insert(mfa_recovery_codes),
        [{"user_id": user_id, "code_hash": _recovery_hash(keys, user_id, c)} for c in codes],
    )
    return codes


async def confirm_enrollment(
    db: AsyncSession,
    keys: Keys,
    ring: KeyRing,
    user_id: UUID,
    secret: str,
    generation: str,
    code: str,
) -> list[str] | None:
    """Store the secret (the envelope form, ADR 0003 section 5) once ``code`` is valid for
    it, and issue recovery codes.

    A first enrollment (``generation == NO_TOTP``) inserts and does nothing if a row appeared
    meanwhile; a replacement updates only the row it was started against. Either way a lost
    race raises ``EnrollmentConflict`` and nothing is stored. A wrong code returns ``None``.
    """
    step = totp.match_step(secret, code)
    if step is None:
        return None
    values = {
        **(await seal_totp_secret(db, ring, user_id, secret.encode())),
        "confirmed_at": func.now(),
        "last_step": step,
    }
    if generation == NO_TOTP:
        stored = await db.execute(
            pg_insert(mfa_totp)
            .values(user_id=user_id, **values)
            .on_conflict_do_nothing(index_elements=[mfa_totp.c.user_id])
            .returning(mfa_totp.c.id)
        )
    else:
        stored = await db.execute(
            update(mfa_totp)
            .where(
                mfa_totp.c.user_id == user_id,
                mfa_totp.c.confirmed_at.is_not(None),
                mfa_totp.c.nonce == bytes.fromhex(generation),
            )
            .values(**values)
            .returning(mfa_totp.c.id)
        )
    if stored.first() is None:
        raise EnrollmentConflict
    return await regenerate_recovery_codes(db, keys, user_id)


async def remove_totp(db: AsyncSession, user_id: UUID) -> bool:
    """Delete the TOTP and the recovery codes. The ``mfa_totp`` row goes first (it is locked),
    the same order as ``regenerate_recovery_codes``, so the two cannot deadlock."""
    result = await db.execute(
        delete(mfa_totp).where(mfa_totp.c.user_id == user_id).returning(mfa_totp.c.id)
    )
    removed = result.first() is not None
    await db.execute(delete(mfa_recovery_codes).where(mfa_recovery_codes.c.user_id == user_id))
    return removed


async def _verify_totp(
    db: AsyncSession, keys: Keys, ring: KeyRing, user_id: UUID, code: str
) -> bool:
    """Raises ``TotpUnreadable`` when the stored secret cannot be decrypted (fails closed)."""
    row = (
        await db.execute(
            select(
                mfa_totp.c.secret_ciphertext,
                mfa_totp.c.nonce,
                mfa_totp.c.key_id,
                mfa_totp.c.wrapped_dek,
                mfa_totp.c.dek_nonce,
            ).where(mfa_totp.c.user_id == user_id, mfa_totp.c.confirmed_at.is_not(None))
        )
    ).first()
    if row is None:
        return False
    secret = (await open_totp_secret(db, ring, user_id, row)).decode()
    step = totp.match_step(secret, code)
    if step is None:
        return False
    # Replay protection: a step is accepted once. The loser of a race updates no row.
    claimed = await db.execute(
        update(mfa_totp)
        .where(mfa_totp.c.user_id == user_id, mfa_totp.c.last_step < step)
        .values(last_step=step)
        .returning(mfa_totp.c.id)
    )
    return claimed.first() is not None


async def _use_recovery_code(db: AsyncSession, keys: Keys, user_id: UUID, code: str) -> bool:
    used = await db.execute(
        update(mfa_recovery_codes)
        .where(
            mfa_recovery_codes.c.user_id == user_id,
            mfa_recovery_codes.c.code_hash == _recovery_hash(keys, user_id, code),
            mfa_recovery_codes.c.used_at.is_(None),
        )
        .values(used_at=func.now())
        .returning(mfa_recovery_codes.c.id)
    )
    return used.first() is not None


async def check_second_factor(
    db: AsyncSession, keys: Keys, ring: KeyRing, user_id: UUID, code: str
) -> Method | None:
    """A TOTP code (six digits) or a recovery code, consumed on success. ``None`` otherwise.
    ``TotpUnreadable`` when a TOTP code meets a secret that cannot be decrypted."""
    code = code.strip()
    if totp.is_totp_code(code):
        return "totp" if await _verify_totp(db, keys, ring, user_id, code) else None
    if await _use_recovery_code(db, keys, user_id, code):
        return "recovery"
    return None


async def record_failure(db: AsyncSession, user_id: UUID, ip: str | None, purpose: str) -> bool:
    """Write ``mfa_failed``. True when this is the fifth in the last hour (mail the user).
    Serialized per user (advisory-lock namespace ``MFA_FAILURES``, ADR 0005 section 9), so
    exactly one of several concurrent failures sees the fifth."""
    await db.execute(
        select(func.pg_advisory_xact_lock(MFA_FAILURES, func.hashtext(cast(user_id, Text))))
    )
    await db.execute(
        insert(auth_events).values(
            kind="mfa_failed", user_id=user_id, ip=ip, detail={"purpose": purpose}
        )
    )
    count = (
        await db.execute(
            select(func.count())
            .select_from(auth_events)
            .where(
                auth_events.c.user_id == user_id,
                auth_events.c.kind == "mfa_failed",
                auth_events.c.at > func.now() - func.make_interval(0, 0, 0, 0, 1),
            )
        )
    ).scalar_one()
    return count == FAILURE_MAIL_THRESHOLD
