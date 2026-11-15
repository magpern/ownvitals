"""The database side of ``/api/profile``: one ``user_profiles`` row per owner, read and written
as the signed-in user under forced RLS (every statement also filters ``owner_id``, ADR 0002).
One of the exact modules of ``ownvitals.views`` that may open a ``user_session``
(``tests/session_scan.py``). It also reads the two effective switches, which sit in a system
table that any session can read.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.config import Settings
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.flags import effective
from ownvitals.views.profile_models import DEFAULTS, ProfileIn, ProfileOut

_SELECT = text(
    "SELECT birth_year, sex, day_assignment, day_tz, theme, weight_unit, temperature_unit, "
    "datetime_format, chart_range, overview_order, show_unsupported, show_experimental, "
    "notify_email, notify_weekly, notify_tiers FROM user_profiles WHERE owner_id = :o"
)
_UPSERT = text(
    "INSERT INTO user_profiles (owner_id, birth_year, sex, day_assignment, day_tz, theme, "
    "weight_unit, temperature_unit, datetime_format, chart_range, overview_order, "
    "show_unsupported, show_experimental, notify_email, notify_weekly, notify_tiers) "
    "VALUES (:o, :birth_year, :sex, :day_assignment, :day_tz, :theme, :weight_unit, "
    ":temperature_unit, :datetime_format, :chart_range, CAST(:overview_order AS text[]), "
    ":show_unsupported, :show_experimental, :notify_email, :notify_weekly, "
    "CAST(:notify_tiers AS text[])) "
    "ON CONFLICT (owner_id) DO UPDATE SET birth_year = EXCLUDED.birth_year, "
    "sex = EXCLUDED.sex, day_assignment = EXCLUDED.day_assignment, day_tz = EXCLUDED.day_tz, "
    "theme = EXCLUDED.theme, weight_unit = EXCLUDED.weight_unit, "
    "temperature_unit = EXCLUDED.temperature_unit, datetime_format = EXCLUDED.datetime_format, "
    "chart_range = EXCLUDED.chart_range, overview_order = EXCLUDED.overview_order, "
    "show_unsupported = EXCLUDED.show_unsupported, "
    "show_experimental = EXCLUDED.show_experimental, notify_email = EXCLUDED.notify_email, "
    "notify_weekly = EXCLUDED.notify_weekly, notify_tiers = EXCLUDED.notify_tiers, "
    "updated_at = now()"
)


async def _stored(db: AsyncSession, ctx: UserContext) -> ProfileIn:
    row = (await db.execute(_SELECT, {"o": ctx.user_id})).mappings().first()
    return ProfileIn.model_validate(dict(row) if row is not None else DEFAULTS)


async def read_profile(
    factory: async_sessionmaker[AsyncSession], ctx: UserContext, settings: Settings
) -> ProfileOut:
    async with user_session(factory, ctx) as db:
        stored = await _stored(db, ctx)
        experimental = await effective.experimental_findings(db, settings)
        ai = await effective.ai_insights(db, settings)
    return ProfileOut(**stored.model_dump(), experimental_available=experimental, ai_available=ai)


async def write_profile(
    factory: async_sessionmaker[AsyncSession],
    ctx: UserContext,
    settings: Settings,
    new: ProfileIn,
) -> tuple[ProfileIn, ProfileOut] | None:
    """Store ``new``; returns the profile it replaced and the stored one, or ``None`` when it asks
    for the experimental results while the switch is off for this account."""
    async with user_session(factory, ctx) as db:
        experimental = await effective.experimental_findings(db, settings)
        old = await _stored(db, ctx)
        if new.show_experimental and not old.show_experimental and not experimental:
            return None
        await db.execute(_UPSERT, {"o": ctx.user_id, **new.model_dump()})
        ai = await effective.ai_insights(db, settings)
    return old, ProfileOut(**new.model_dump(), experimental_available=experimental, ai_available=ai)
