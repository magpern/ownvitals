"""The signed-in user's own email preferences (``email_prefs``; ADR 0018, #459).

Read and written in the caller's own session (``user_session``, forced RLS): a row of another
owner is neither seen nor written. No row is "on". Every change of a kind's choice counts in its
``generation``, so an unsubscribe link sent before the change stops working (ADR 0018). Only the
suppressible kinds (``ownvitals.notify.email_policy``) have a preference; the database's check
refuses any other.
"""

from collections.abc import Mapping
from types import MappingProxyType

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.notify.email_policy import SUPPRESSIBLE_KINDS

Factory = async_sessionmaker[AsyncSession]

_READ = text("SELECT kind, enabled FROM email_prefs WHERE owner_id = :o")
# A change of the choice bumps the generation; saving the same choice again changes nothing.
_SET = text(
    "INSERT INTO email_prefs AS e (owner_id, kind, enabled, generation) "
    "VALUES (:o, :kind, :enabled, 1) "
    "ON CONFLICT (owner_id, kind) DO UPDATE "
    "SET enabled = EXCLUDED.enabled, generation = e.generation + 1, updated_at = now() "
    "WHERE e.enabled IS DISTINCT FROM EXCLUDED.enabled"
)


def _view(found: Mapping[str, bool]) -> Mapping[str, bool]:
    return MappingProxyType({kind: found.get(kind, True) for kind in SUPPRESSIBLE_KINDS})


async def read(factory: Factory, ctx: UserContext) -> Mapping[str, bool]:
    """Each suppressible kind and whether its email is on."""
    async with user_session(factory, ctx) as db:
        rows = (await db.execute(_READ, {"o": ctx.user_id})).all()
    return _view({str(r.kind): bool(r.enabled) for r in rows})


async def set_enabled(
    factory: Factory, ctx: UserContext, kind: str, enabled: bool
) -> Mapping[str, bool]:
    """Switch the caller's email of ``kind`` on or off; the preferences after the change.
    ``ValueError`` for a kind that is not suppressible."""
    if kind not in SUPPRESSIBLE_KINDS:
        raise ValueError("not a suppressible mail kind")
    async with user_session(factory, ctx) as db:
        await db.execute(_SET, {"o": ctx.user_id, "kind": kind, "enabled": enabled})
        rows = (await db.execute(_READ, {"o": ctx.user_id})).all()
    return _view({str(r.kind): bool(r.enabled) for r in rows})
