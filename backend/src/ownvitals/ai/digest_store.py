"""The weekly digest, web side (ADR 0014 sections 3 and 5; PLAN section 7.4; issue 209): read the
user's stored digests and set the digest opt-in. Every function acts for one ``UserContext`` in
its ``user_session`` (forced RLS) and every statement also names the owner (ADR 0002). Web never
calls the model and never queues a digest (``ai.digest`` in the worker does).

- **Read** (``read_view``): whether AI is enabled for the user (the effective flag and a live
  consent to the current text), whether they opted in, and their newest digests (also while AI
  is off, as the chat history is: withdrawal is what deletes them).
- **Opt in** (``set_opt_in(..., True)``): only while AI is enabled for the user, checked in the
  same transaction (``consent.require_enabled``, the consent row ``FOR SHARE``, so a withdrawal
  waits for this commit and then deletes the opt-in, or commits first and this refuses). No
  step-up: the opt-in is covered by the consent given with one (ADR 0014 section 3).
- **Opt out** (``set_opt_in(..., False)``): always, also while AI is off; it stops the next
  digests and keeps the stored ones.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.ai import consent
from ownvitals.ai.digest_model import LIST_LIMIT
from ownvitals.config import Settings
from ownvitals.context import UserContext, require_context
from ownvitals.db import user_session

Factory = async_sessionmaker[AsyncSession]

_OPTED_IN: Final = text("SELECT 1 FROM ai_digest_prefs WHERE owner_id = :o")
_OPT_IN: Final = text(
    "INSERT INTO ai_digest_prefs (owner_id) VALUES (:o) ON CONFLICT (owner_id) DO NOTHING"
)
_OPT_OUT: Final = text("DELETE FROM ai_digest_prefs WHERE owner_id = :o")
_NEWEST: Final = text(
    "SELECT week_start, status, content, created_at FROM ai_digests WHERE owner_id = :o "
    "ORDER BY week_start DESC LIMIT :limit"
)


@dataclass(frozen=True, slots=True)
class Digest:
    week_start: date
    status: str
    content: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DigestView:
    available: bool  # the flag and a live consent to the current text
    opted_in: bool
    digests: list[Digest]


async def _available(db: AsyncSession, ctx: UserContext, settings: Settings) -> bool:
    try:
        await consent.require_enabled(db, ctx.user_id, settings)
    except consent.AiNotEnabled:
        return False
    return True


async def read_view(factory: Factory, ctx: UserContext, settings: Settings) -> DigestView:
    """The user's digest state and newest digests."""
    require_context(ctx)
    params = {"o": ctx.user_id}
    async with user_session(factory, ctx) as db:
        available = await _available(db, ctx, settings)
        opted_in = (await db.execute(_OPTED_IN, params)).first() is not None
        rows = (await db.execute(_NEWEST, {**params, "limit": LIST_LIMIT})).all()
    digests = [
        Digest(r.week_start, str(r.status), None if r.content is None else str(r.content),
               r.created_at)
        for r in rows
    ]  # fmt: skip
    return DigestView(available, opted_in, digests)


async def set_opt_in(factory: Factory, ctx: UserContext, settings: Settings, on: bool) -> bool:
    """Opt in (``consent.AiNotEnabled`` unless AI is enabled for the user) or out; the new
    state."""
    require_context(ctx)
    params = {"o": ctx.user_id}
    async with user_session(factory, ctx) as db:
        if on:
            await consent.require_enabled(db, ctx.user_id, settings)  # the consent row first
            await db.execute(_OPT_IN, params)
        else:
            await db.execute(_OPT_OUT, params)
    return on
