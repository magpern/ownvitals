"""AI consent: give, withdraw, and the check every AI entry point makes (ADR 0014 sections 1 and
3; PLAN section 7.4; #206).

Every function acts for one ``UserContext`` in its ``user_session`` (forced RLS) and every
statement also names the owner (ADR 0002). Nothing here takes an owner id from a caller.

- **The check** (``require_enabled``): the effective ``ai_insights`` flag (environment and
  stored, the stricter wins) **and** a live consent to the current text (``consent_text``:
  version, language and SHA-256). Otherwise ``AiNotEnabled``, which every entry point answers as
  "not enabled". ``require_consent`` is the consent half alone, in the caller's own transaction
  (a job's fenced session): it reads the live row ``FOR SHARE``, so a reply stored in that
  transaction and a withdrawal serialise on the row (ADR 0014 section 3).
- **Give** (step-up at the route): only while the flag is on, only for the exact current text.
  A live consent to an older version is closed and replaced; one to the current text is kept.
- **Withdraw** (no step-up, and also while the flag is off: leaving must be easy), in one
  transaction and in this order: (1) close the live consent row (its row lock comes first),
  (2) cancel the user's queued and running AI jobs (a queued one ends at once, a running one at
  its next checkpoint; the job's own ``require_consent`` refuses as well), (3) delete the
  conversation history. The spend ledger (``ai.budget``) and the consent rows stay; they hold no
  content.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.ai import consent_text
from ownvitals.ai.tables import HISTORY_TABLES
from ownvitals.config import Settings
from ownvitals.context import UserContext, require_context
from ownvitals.db import user_session
from ownvitals.flags import effective
from ownvitals.jobs import user as user_jobs

Factory = async_sessionmaker[AsyncSession]
Reason = Literal["feature_off", "no_consent", "consent_outdated"]

# The AI job kinds of ADR 0014 section 5 (registered by the chat and digest issues); withdrawal
# cancels the user's queued and running jobs of these kinds.
AI_JOB_KINDS: Final = ("ai_chat_turn", "ai_digest")

_LIVE = text(
    "SELECT consent_version, locale, text_sha256, given_at FROM ai_consents "
    "WHERE owner_id = :o AND withdrawn_at IS NULL"
)
_LIVE_FOR_SHARE = text(
    "SELECT consent_version, locale, text_sha256 FROM ai_consents "
    "WHERE owner_id = :o AND withdrawn_at IS NULL FOR SHARE"
)
_LIVE_FOR_SHARE_NOWAIT = text(
    "SELECT consent_version, locale, text_sha256 FROM ai_consents "
    "WHERE owner_id = :o AND withdrawn_at IS NULL FOR SHARE NOWAIT"
)
_LOCK_NOT_AVAILABLE: Final = "55P03"
_LIVE_FOR_UPDATE = text(
    "SELECT id, consent_version, locale, text_sha256 FROM ai_consents "
    "WHERE owner_id = :o AND withdrawn_at IS NULL FOR UPDATE"
)
_CLOSE = text(
    "UPDATE ai_consents SET withdrawn_at = now() "
    "WHERE owner_id = :o AND withdrawn_at IS NULL RETURNING id"
)
_INSERT = text(
    "INSERT INTO ai_consents (owner_id, consent_version, locale, text_sha256) "
    "VALUES (:o, :v, :l, :d) ON CONFLICT (owner_id) WHERE withdrawn_at IS NULL DO NOTHING "
    "RETURNING id"
)
_AI_JOBS = text(
    "SELECT id, cancel_requested_at FROM jobs WHERE owner_id = :o "
    "AND kind = ANY(CAST(:kinds AS text[])) AND state IN ('queued', 'running') ORDER BY id"
)


class AiNotEnabled(Exception):
    """AI is not enabled for this user: the flag is off, or there is no live consent to the
    current text. ``reason`` is for logs and tests; every entry point answers "not enabled"."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


class ConsentMismatch(Exception):
    """The version, language or fingerprint sent is not the current consent text."""


@dataclass(frozen=True, slots=True)
class ConsentState:
    """The user's live consent; ``current`` is false for an older text (consent again)."""

    version: int
    locale: str
    given_at: datetime
    current: bool


@dataclass(frozen=True, slots=True)
class Withdrawn:
    """What a withdrawal did (counts only)."""

    closed: bool
    cancelled_jobs: int
    deleted: dict[str, int]


async def read_state(factory: Factory, ctx: UserContext) -> ConsentState | None:
    """The user's live consent, or ``None``."""
    async with user_session(factory, ctx) as db:
        row = (await db.execute(_LIVE, {"o": ctx.user_id})).first()
    if row is None:
        return None
    current = consent_text.matches(
        int(row.consent_version), str(row.locale), bytes(row.text_sha256)
    )
    return ConsentState(int(row.consent_version), str(row.locale), row.given_at, current)


async def feature_on(factory: Factory, ctx: UserContext, settings: Settings) -> bool:
    """The effective ``ai_insights`` flag, read as the user."""
    async with user_session(factory, ctx) as db:
        return await effective.ai_insights(db, settings)


async def require_consent(db: AsyncSession, owner: UUID, *, nowait: bool = False) -> None:
    """In the caller's transaction (a job's fenced session): a live consent to the current text,
    read ``FOR SHARE`` and held until the commit; else ``AiNotEnabled``.

    ``nowait`` is for a transaction that already holds its job row (a fenced session: the fence
    is its first statement). A withdrawal locks the consent row first and the job rows after it,
    so waiting here would close a lock cycle; with ``nowait`` a consent row another transaction
    holds (a withdrawal under way) answers ``AiNotEnabled`` at once, and the caller's transaction
    is rolled back, releasing its job row to the withdrawal."""
    statement = _LIVE_FOR_SHARE_NOWAIT if nowait else _LIVE_FOR_SHARE
    try:
        row = (await db.execute(statement, {"o": owner})).first()
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE:
            raise AiNotEnabled("no_consent") from None
        raise
    if row is None:
        raise AiNotEnabled("no_consent")
    if not consent_text.matches(int(row.consent_version), str(row.locale), bytes(row.text_sha256)):
        raise AiNotEnabled("consent_outdated")


async def require_enabled(
    db: AsyncSession, owner: UUID, settings: Settings, *, nowait: bool = False
) -> None:
    """The flag, then ``require_consent``, in the caller's transaction."""
    if not await effective.ai_insights(db, settings):
        raise AiNotEnabled("feature_off")
    await require_consent(db, owner, nowait=nowait)


async def check_enabled(factory: Factory, ctx: UserContext, settings: Settings) -> None:
    """``require_enabled`` in a transaction of its own; raises ``AiNotEnabled``."""
    async with user_session(factory, ctx) as db:
        await require_enabled(db, ctx.user_id, settings)


async def give(
    factory: Factory,
    ctx: UserContext,
    settings: Settings,
    *,
    version: int,
    locale: str,
    digest: bytes,
) -> ConsentState:
    """Record consent to the current text in ``locale``. ``AiNotEnabled("feature_off")`` while
    the flag is off (checked first, whatever the text); ``ConsentMismatch`` for any other
    text."""
    async with user_session(factory, ctx) as db:
        if not await effective.ai_insights(db, settings):
            raise AiNotEnabled("feature_off")
        if not consent_text.matches(version, locale, digest):
            raise ConsentMismatch
        params = {"o": ctx.user_id}
        live = (await db.execute(_LIVE_FOR_UPDATE, params)).first()
        same = live is not None and (
            int(live.consent_version),
            str(live.locale),
            bytes(live.text_sha256),
        ) == (version, locale, digest)
        if live is not None and not same:  # an older text or another language: replaced
            await db.execute(_CLOSE, params)
        if not same:
            # A concurrent give may have taken the live slot first: then it stays, and the
            # state below is that row's.
            await db.execute(_INSERT, {**params, "v": version, "l": locale, "d": digest})
        row = (await db.execute(_LIVE, params)).one()
    current = consent_text.matches(
        int(row.consent_version), str(row.locale), bytes(row.text_sha256)
    )
    return ConsentState(int(row.consent_version), str(row.locale), row.given_at, current)


async def withdraw(factory: Factory, ctx: UserContext) -> Withdrawn:
    """Close the live consent, cancel the user's AI jobs and delete the conversation history,
    in one transaction (the consent row's lock first, then the job rows)."""
    require_context(ctx)
    params = {"o": ctx.user_id}
    deleted: dict[str, int] = {}
    async with user_session(factory, ctx) as db:
        closed = (await db.execute(_CLOSE, params)).first() is not None
        jobs = (await db.execute(_AI_JOBS, {**params, "kinds": list(AI_JOB_KINDS)})).all()
        cancelled = 0
        for job in jobs:
            # Counted only when this withdrawal cancels it: a running job whose cancel was
            # already requested is not counted again.
            if (
                await user_jobs.cancel(db, ctx, job.id) is not None
                and job.cancel_requested_at is None
            ):
                cancelled += 1
        for table in HISTORY_TABLES:
            result = await db.execute(delete(table).where(table.c.owner_id == ctx.user_id))
            deleted[table.name] = int(result.rowcount)  # pyright: ignore[reportAttributeAccessIssue]
    return Withdrawn(closed=closed, cancelled_jobs=cancelled, deleted=deleted)
