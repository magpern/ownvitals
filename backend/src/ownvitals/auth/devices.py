"""Known devices (ADR 0003 section 3).

A successful sign-in sets ``__Host-ownvitals_device`` (32 random bytes, kept for a year). Per
user the database holds only ``HMAC(device_key, user_id || token)``. A browser shared by several
users holds one token, which can be a known device for each of them. The only effect is to
exempt the user from the per-account rate cap; a stolen token grants nothing else.
"""

from uuid import UUID

from sqlalchemy import delete, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.keys import Keys, hmac_digest, new_token
from ownvitals.auth.tables import known_devices


def device_hash(keys: Keys, user_id: UUID, token: str) -> bytes:
    return hmac_digest(keys.device, str(user_id), token)


async def is_known_device(db: AsyncSession, keys: Keys, user_id: UUID, token: str | None) -> bool:
    """True when ``token`` is a known device of ``user_id``; refreshes its ``last_used_at``."""
    if not token:
        return False
    result = await db.execute(
        update(known_devices)
        .where(
            known_devices.c.user_id == user_id,
            known_devices.c.device_hash == device_hash(keys, user_id, token),
        )
        .values(last_used_at=func.now())
        .returning(known_devices.c.id)
    )
    return result.first() is not None


async def remember_device(db: AsyncSession, keys: Keys, user_id: UUID, token: str | None) -> str:
    """Make the browser's device token (or a new one) a known device of ``user_id``.

    Returns the token for the cookie.
    """
    token = token or new_token()
    statement = pg_insert(known_devices).values(
        user_id=user_id, device_hash=device_hash(keys, user_id, token)
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[known_devices.c.device_hash], set_={"last_used_at": func.now()}
        )
    )
    return token


async def forget_devices(db: AsyncSession, user_id: UUID) -> int:
    """Delete every known device of the user (a reset does this)."""
    result = await db.execute(delete(known_devices).where(known_devices.c.user_id == user_id))
    return int(result.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue]
