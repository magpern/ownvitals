"""The flags as one request sees them: the environment joined with the stored flags (ADR 0003
section 6; ``ownvitals.flags.core``). Read per request; nothing is cached.

``app_settings`` is a system table (ADR 0002 section 3): the app role may only read it, in any
session; the admin area writes it as ``ownvitals_maint``. A cleared flag is stored as the JSON
``null`` (maint has no ``DELETE``), which reads as no row.
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.config import Settings
from ownvitals.flags import caps, core
from ownvitals.flags.caps import Caps
from ownvitals.flags.core import GLOBAL_CAP, USER_CAP, Banner, SignupMode

_ONE = text("SELECT value FROM app_settings WHERE flag = :key")
_CAPS = text("SELECT flag, value FROM app_settings WHERE flag IN (:user, :global)")


async def _stored(db: AsyncSession, key: str) -> Any:
    return (await db.execute(_ONE, {"key": key})).scalar_one_or_none()


async def signup_mode(db: AsyncSession, environment: SignupMode) -> SignupMode:
    """The stricter of ``OWNVITALS_SIGNUP_MODE`` and the stored ``signup_mode``."""
    return core.stricter_signup(environment, await _stored(db, "signup_mode"))


async def experimental_findings(db: AsyncSession, settings: Settings) -> bool:
    """D9's owner-only experimental tier switch: on only when the environment and the stored
    flag both allow it (default off)."""
    if not settings.experimental_findings:  # the environment's switch: no read needed
        return False
    return core.stricter_switch(
        "experimental_findings", True, await _stored(db, "experimental_findings")
    )


async def ai_insights(db: AsyncSession, settings: Settings) -> bool:
    """D11's AI insights switch: on only when the environment and the stored flag both allow it
    (default off)."""
    if not settings.ai_insights:
        return False
    return core.stricter_switch("ai_insights", True, await _stored(db, "ai_insights"))


async def banner(db: AsyncSession) -> Banner | None:
    return core.banner_setting(await _stored(db, "banner"))


async def ai_caps(db: AsyncSession, settings: Settings) -> Caps:
    """The AI monthly caps in force (``ownvitals.flags.caps``): the environment's, lowered by the
    stored ones. Read in the caller's transaction, so ``ai_reserve`` gets what was stored when it
    ran. A failed read raises: the caller makes no reservation (fail closed); an invalid stored
    value counts as 0."""
    rows = (await db.execute(_CAPS, {"user": USER_CAP, "global": GLOBAL_CAP})).all()
    stored = {r.flag: r.value for r in rows}
    return caps.effective_caps(
        settings.ai_user_monthly_cap_microusd,
        settings.ai_global_monthly_cap_microusd,
        stored.get(USER_CAP),
        stored.get(GLOBAL_CAP),
    )
