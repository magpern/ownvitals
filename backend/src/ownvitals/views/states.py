"""The data states of the design ("one component, one wording, everywhere"; docs/design/README.md)
and the rules that pick one. Pure: facts in, a state out; no database, clock or Withings.

States: ``up_to_date``, ``stale``, ``importing`` (with a percentage), ``incomplete`` (an import
that stopped before its end), ``insufficient_data`` (with progress toward the minimum),
``not_supported``, ``not_shared``, ``not_available``, ``needs_reconnect``,
``withings_plus_needed`` and ``not_connected``.

A stream's state comes first (the connection, then the capability the sync learned, then the
import, then freshness); a metric adds ``insufficient_data`` and the recompute-pending ``stale``.
A stream that is not supported, not shared or not available is **never** reported as "no data":
its state says what it is and no count is given for it.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Final

from ownvitals.analysis.policy import Requirement, Shortfall
from ownvitals.views.catalog import PLAN_BLOCK, STALE_AFTER_HOURS
from ownvitals.withings.capabilities import NOT_AUTHORIZED, NOT_AVAILABLE, UNSUPPORTED

UP_TO_DATE: Final = "up_to_date"
STALE: Final = "stale"
IMPORTING: Final = "importing"
INCOMPLETE: Final = "incomplete"
INSUFFICIENT: Final = "insufficient_data"
NOT_SUPPORTED: Final = "not_supported"
NOT_SHARED: Final = "not_shared"
NOT_AVAILABLE_STATE: Final = "not_available"
NEEDS_RECONNECT: Final = "needs_reconnect"
PLUS_NEEDED: Final = "withings_plus_needed"
NOT_CONNECTED: Final = "not_connected"

STATES: Final = (
    UP_TO_DATE,
    STALE,
    IMPORTING,
    INCOMPLETE,
    INSUFFICIENT,
    NOT_SUPPORTED,
    NOT_SHARED,
    NOT_AVAILABLE_STATE,
    NEEDS_RECONNECT,
    PLUS_NEEDED,
    NOT_CONNECTED,
)
# States that say what the stream is; nothing computed from it is shown as a value.
BLOCKED: Final = frozenset(
    {NOT_SUPPORTED, NOT_SHARED, NOT_AVAILABLE_STATE, NEEDS_RECONNECT, PLUS_NEEDED, NOT_CONNECTED}
)
_BY_CAPABILITY: Final = {
    NOT_AUTHORIZED: NOT_SHARED,
    UNSUPPORTED: NOT_SUPPORTED,
    NOT_AVAILABLE: NOT_AVAILABLE_STATE,
}


@dataclass(frozen=True, slots=True)
class ConnectionFacts:
    """The user's live Withings connection; ``None`` where the user has none."""

    status: str  # connected | needs_reconnect
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class StreamFacts:
    """One stream's cursor on the live connection (``sync_cursors``) and whether a backfill job
    for it is queued or running. ``capability`` and ``backfill_status`` are ``None`` before the
    connect job has written the cursor."""

    stream: str
    capability: str | None = None
    backfill_status: str | None = None  # running | complete
    windows_done: int = 0
    windows_total: int = 0
    import_job_active: bool = False
    last_ok_at: datetime | None = None
    last_error_category: str | None = None
    skipped_out_of_range: int = 0


@dataclass(frozen=True, slots=True)
class StreamState:
    state: str
    progress_percent: int | None = None  # importing only


def plan_blocked(connection: ConnectionFacts | None, facts: StreamFacts | None) -> bool:
    return (connection is not None and connection.last_error == PLAN_BLOCK) or (
        facts is not None and facts.last_error_category == PLAN_BLOCK
    )


def import_percent(facts: StreamFacts) -> int:
    if facts.windows_total <= 0:
        return 0
    return max(0, min(99, facts.windows_done * 100 // facts.windows_total))


def stream_state(
    connection: ConnectionFacts | None, facts: StreamFacts | None, now: datetime
) -> StreamState:
    """The state of one stream, in this order: no connection, a broken one, the Withings+ block,
    the capability (what the grant and the API allow), the import, then freshness."""
    if connection is None:
        return StreamState(NOT_CONNECTED)
    if connection.status == "needs_reconnect":
        return StreamState(NEEDS_RECONNECT)
    if plan_blocked(connection, facts):
        return StreamState(PLUS_NEEDED)
    if facts is None:  # the connect job has not written the cursors yet
        return StreamState(IMPORTING, 0)
    blocked = _BY_CAPABILITY.get(facts.capability or "")
    if blocked is not None:
        return StreamState(blocked)
    if facts.backfill_status != "complete":
        if facts.import_job_active or facts.backfill_status is None:
            return StreamState(IMPORTING, import_percent(facts))
        return StreamState(INCOMPLETE, import_percent(facts))
    if facts.last_ok_at is not None and now - facts.last_ok_at > timedelta(hours=STALE_AFTER_HOURS):
        return StreamState(STALE)
    return StreamState(UP_TO_DATE)


@dataclass(frozen=True, slots=True)
class Progress:
    """How far a series is from the minimum-data policy's requirement."""

    days_with_data: int
    days_required: int
    span_days: int
    span_required: int | None
    shortfalls: tuple[str, ...]
    fraction: float  # 0..1, the least advanced of the requirement's parts


def progress_toward(
    requirement: Requirement,
    shortfalls: tuple[Shortfall, ...],
    *,
    n_days: int,
    span_days: int,
    window_days: int,
) -> Progress:
    """``n_days`` days with data and ``span_days`` days elapsed against ``requirement``."""
    span_required = requirement.min_span_days
    if requirement.min_span_fraction is not None:
        needed = math.ceil(requirement.min_span_fraction * window_days)
        span_required = needed if span_required is None else max(span_required, needed)
    parts = [Fraction(n_days, requirement.min_days_with_data)]
    if span_required:
        parts.append(Fraction(span_days, span_required))
    return Progress(
        days_with_data=n_days,
        days_required=requirement.min_days_with_data,
        span_days=span_days,
        span_required=span_required,
        shortfalls=tuple(s.value for s in shortfalls),
        fraction=round(float(min(min(parts), Fraction(1))), 4),
    )
