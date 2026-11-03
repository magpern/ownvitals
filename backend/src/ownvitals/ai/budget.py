"""The per-user monthly AI cost cap (D11, ADR 0014 section 7).

Every model request is reserved before it is sent and settled or released after, through the
``SECURITY DEFINER`` functions of ``docker/postgres/ai/50-ai.sql`` (owned by ``ownvitals_ai``;
the app role only reads its own ``ai_spend`` and ``ai_reservations`` rows and has no privilege
on ``ai_spend_global``). Units are integer micro-USD; a month is the calendar month in UTC.

The worker's flow for one request of a job (``fs`` is that job's ``fenced_session``):

1. ``reserve(fs, bound, settings)`` with the request's upper bound (``ownvitals.ai.pricing``),
   in its own fenced transaction that **commits before the call**. ``None``: a cap refused it,
   so no call is made ("AI paused until <date>", ``paused_message``).
2. After the call, in a new fenced transaction:
   - a completed call: ``settle(fs, reservation, actual)`` with the cost of the reported usage;
     an actual above the reservation is charged in full and returns ``OVERRUN``;
   - ``may_release(sent=..., status=...)`` true (the request provably was not processed):
     ``release(fs, reservation)``;
   - anything else (a timeout, a broken connection after sending, any other 5xx): settle at the
     reserved amount, ``settle(fs, reservation, reservation.amount)``, because it may have been
     billed.
3. A worker that dies, or loses its lease, between 1 and 2 leaves the reservation open; the
   worker's maintenance calls ``settle_lost``, which settles it at the full amount once the job
   attempt is over. A second settle, or a settle after the reaper, changes nothing.

The caps are the settings ``ai_user_monthly_cap_microusd`` and ``ai_global_monthly_cap_microusd``
(both 0 until the owner sets them, ADR 0014 open question 2), passed on every reservation: the
settings are authoritative and nothing stores a cap. Logs carry counts and outcomes, never
amounts tied to a user, never content.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.config import Settings
from ownvitals.context import UserContext, require_context
from ownvitals.db import system_session
from ownvitals.jobs.fence import FencedSession

logger = logging.getLogger(__name__)

Factory = async_sessionmaker[AsyncSession]

# The owned tables of migration 0013; ``ai_spend_global`` is a system table (``SYSTEM_TABLES``).
AI_OWNED: Final = ("ai_reservations", "ai_spend")

# The function's own bounds (50-ai.sql): one request at most 100,000 USD.
MAX_AMOUNT: Final = 100_000_000_000
MAX_ACTUAL: Final = 1_000_000_000_000
SETTLE_LOST_LIMIT: Final = 100
# Answers that mean the request was refused or shed before it was processed (ADR 0014 section 7).
RELEASABLE_STATUSES: Final = frozenset({400, 401, 403, 404, 413, 429, 529})

_RESERVE = text("SELECT public.ai_reserve(:amount, :job_id, :user_cap, :global_cap)")
_SETTLE = text("SELECT public.ai_settle(:id, :actual)")
_RELEASE = text("SELECT public.ai_release(:id)")
_SETTLE_LOST = text("SELECT public.ai_settle_lost(:n)")
_MONTH = text(
    "SELECT reserved, spent FROM public.ai_spend WHERE owner_id = :owner AND month = :month"
)


@dataclass(frozen=True, slots=True)
class Reservation:
    """An open reservation: its id and the amount reserved, in micro-USD."""

    id: UUID
    amount: int


class Settled(StrEnum):
    SETTLED = "settled"
    # The actual was above the reservation (the bound was wrong): charged in full anyway.
    OVERRUN = "overrun"
    # Nothing was open any more (already settled, released or reaped): nothing changed.
    UNCHANGED = "unchanged"


def _amount(value: int, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high} micro-USD")
    return value


def month_start(now: datetime) -> date:
    """The first day of ``now``'s calendar month in UTC (the month a reservation counts in)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    utc = now.astimezone(UTC)
    return date(utc.year, utc.month, 1)


def resets_on(now: datetime) -> date:
    """The first day of the next calendar month in UTC: when the budget starts again."""
    start = month_start(now)
    return date(start.year + start.month // 12, start.month % 12 + 1, 1)


def paused_message(reset: date) -> str:
    """The user-facing state at the cap (ADR 0014 section 7)."""
    return (
        "This month's AI budget for your account is used up. "
        f"It resets on {reset.day} {reset:%B %Y}."
    )


async def reserve(fs: FencedSession, amount: int, settings: Settings) -> Reservation | None:
    """Reserve ``amount`` for one request of the fenced job. ``None``: a cap refused it.

    The caller's fenced transaction must commit before the request is sent, so the reservation
    holds while the call runs (and the reaper can find it if the worker dies).
    """
    _amount(amount, "amount", 1, MAX_AMOUNT)
    row = (
        await fs.db.execute(
            _RESERVE,
            {
                "amount": amount,
                "job_id": fs.lease.job_id,
                "user_cap": settings.ai_user_monthly_cap_microusd,
                "global_cap": settings.ai_global_monthly_cap_microusd,
            },
        )
    ).scalar_one()
    if row is None:
        logger.info("ai reservation refused", extra={"event": "ai_budget_refused"})
        return None
    return Reservation(id=row, amount=amount)


async def settle(fs: FencedSession, reservation: Reservation, actual: int) -> Settled:
    """Charge ``actual`` against the reservation, in the reservation's own month."""
    _amount(actual, "actual", 0, MAX_ACTUAL)
    changed = (await fs.db.execute(_SETTLE, {"id": reservation.id, "actual": actual})).scalar_one()
    if not changed:
        return Settled.UNCHANGED
    if actual > reservation.amount:
        # A count only: no amount, no user (ADR 0014 section 6).
        logger.warning("ai cost overrun", extra={"event": "ai_cost_overrun"})
        return Settled.OVERRUN
    return Settled.SETTLED


async def release(fs: FencedSession, reservation: Reservation) -> bool:
    """Give the reservation back: only for a request that provably was not processed."""
    return bool((await fs.db.execute(_RELEASE, {"id": reservation.id})).scalar_one())


def may_release(*, sent: bool, status: int | None) -> bool:
    """Whether a failed request may be released rather than charged (ADR 0014 section 7).

    Released: a connection failure before the request was sent, or an answer of 400, 401, 403,
    404, 413, 429 or 529. Everything else (a timeout, a broken connection after sending, any
    other status) is charged at the reserved amount, because it may have been billed.
    """
    if not sent:
        return True
    return status in RELEASABLE_STATUSES


async def settle_lost(factory: Factory, limit: int = SETTLE_LOST_LIMIT) -> int:
    """The reaper: settle, at their full amount, open reservations whose job attempt is over.

    The cross-user path (ADR 0002 section 4's maintenance context): no user, a count back.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    async with system_session(factory) as db:
        return int((await db.execute(_SETTLE_LOST, {"n": limit})).scalar_one())


@dataclass(frozen=True, slots=True)
class MonthBudget:
    """One user's budget for the current month, in micro-USD."""

    month: date
    spent: int
    reserved: int
    cap: int
    resets_on: date

    @property
    def paused(self) -> bool:
        """At the cap: no further request can be reserved this month."""
        return self.spent + self.reserved >= self.cap

    def message(self) -> str | None:
        return paused_message(self.resets_on) if self.paused else None


async def month_budget(
    db: AsyncSession, ctx: UserContext, settings: Settings, now: datetime
) -> MonthBudget:
    """The user's budget this month. ``db`` is ``ctx``'s ``user_session``: forced RLS returns
    only their row, and the query filters on ``ctx.user_id`` as well."""
    require_context(ctx)
    month = month_start(now)
    row = (await db.execute(_MONTH, {"owner": ctx.user_id, "month": month})).first()
    reserved, spent = (0, 0) if row is None else (int(row.reserved), int(row.spent))
    return MonthBudget(
        month=month,
        spent=spent,
        reserved=reserved,
        cap=settings.ai_user_monthly_cap_microusd,
        resets_on=resets_on(now),
    )
