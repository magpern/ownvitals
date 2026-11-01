"""The flags as one request sees them: the environment joined with the database rows
(ADR 0011 section 7). Read per request; nothing is cached.

``app_settings`` is a system table: the app role may read it in any session (``SELECT`` only,
the admin area writes it as ``ownvitals_maint``).
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.context import UserContext
from ownvitals.flags import core
from ownvitals.flags.core import Banner, SignupMode, SongPicking, TrialSetting

_ALL = text("SELECT key, value FROM app_settings")
_ONE = text("SELECT value FROM app_settings WHERE key = :key")


async def _stored(db: AsyncSession, key: str) -> Any:
    return (await db.execute(_ONE, {"key": key})).scalar_one_or_none()


async def stored_all(db: AsyncSession) -> dict[str, Any]:
    return {row.key: row.value for row in (await db.execute(_ALL)).all()}


async def signup_mode(db: AsyncSession, environment: SignupMode) -> SignupMode:
    """The stricter of ``OWNVITALS_SIGNUP_MODE`` and the stored ``signup_mode``."""
    return core.stricter_signup(environment, await _stored(db, "signup_mode"))


async def song_picking(db: AsyncSession, environment_on: bool) -> SongPicking:
    if not environment_on:  # the kill switch: no read needed
        return "off"
    return core.song_picking_setting(environment_on, await _stored(db, "song_picking"))


async def song_picking_admits(db: AsyncSession, ctx: UserContext, environment_on: bool) -> bool:
    """``off`` refuses everyone; ``on`` admits everyone; ``granted`` only users whose effective
    features include ``song_picking``."""
    from ownvitals.plans import effective_features  # the plans package reads flags (trial)

    mode = await song_picking(db, environment_on)
    if mode == "off":
        return False
    if mode == "on":
        return True
    return "song_picking" in await effective_features(db, ctx)


async def trial(db: AsyncSession) -> TrialSetting | None:
    return core.trial_setting(await _stored(db, "trial"))


async def banner(db: AsyncSession) -> Banner | None:
    return core.banner_setting(await _stored(db, "banner"))
