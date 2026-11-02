"""The sync streams (PLAN section 4.4) and the windows a backfill walks.

Measures and the other full-history series use one-year windows. ECG uses three-month windows
of ``heart list``, then one ``heart get`` per signal. Sleep series covers the last 90 days and
intraday the last 30, one day at a time because those calls accept at most 24 hours; a larger
``demand_days`` on the cursor extends either without changing the default. Devices and goals
are fetched once on connect and again from the daily poll. History starts at 2009-01-01, the
partition floor, and stops at ``now``.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

FLOOR = datetime(2009, 1, 1, tzinfo=UTC)
DAY = 86_400


@dataclass(frozen=True, slots=True)
class Stream:
    """One stream. ``scopes`` is any-of: one matching scope is enough to call."""

    name: str
    scopes: tuple[str, ...]
    shape: str
    history_days: int | None = None


STREAMS: Final[tuple[Stream, ...]] = (
    Stream("measures", ("user.metrics",), "measures"),
    Stream("ecg", ("user.metrics",), "ecg"),
    Stream("activity_daily", ("user.activity",), "activity"),
    Stream("workouts", ("user.activity",), "workouts"),
    Stream("sleep_summary", ("user.activity",), "sleep_summary"),
    Stream("sleep_series", ("user.activity",), "sleep_series", 90),
    Stream("intraday", ("user.activity",), "intraday", 30),
    Stream("devices", ("user.info",), "devices"),
    Stream("goals", ("user.info", "user.metrics"), "goals"),
)
BY_NAME: Final[dict[str, Stream]] = {stream.name: stream for stream in STREAMS}


def stream_named(name: str) -> Stream:
    found = BY_NAME.get(name)
    if found is None:
        raise KeyError(name)
    return found


def _add_months(moment: datetime, months: int) -> datetime:
    index = moment.month - 1 + months
    year = moment.year + index // 12
    month = index % 12 + 1
    return moment.replace(year=year, month=month)


def _step(shape: str) -> tuple[int, int] | None:
    """``(months, days)`` added to walk one window, or ``None`` for a single fetch."""
    if shape in {"measures", "activity", "workouts", "sleep_summary"}:
        return (12, 0)
    if shape == "ecg":
        return (3, 0)
    if shape in {"sleep_series", "intraday"}:
        return (0, 1)
    return None


def windows(
    stream: Stream, now: datetime, *, demand_days: int | None = None
) -> list[tuple[int, int]]:
    """Half-open windows as inclusive ``(start, end)`` epoch seconds the API can be given.

    The end of every window but the last is one second before the next start, so a sample on
    the boundary is fetched once. Devices and goals are one empty marker: the call takes no range.
    """
    step = _step(stream.shape)
    if step is None:
        return [(0, 0)]
    months, days = step
    if stream.history_days is None:
        start = FLOOR
    else:
        span = stream.history_days
        if demand_days is not None and demand_days > span:
            span = demand_days
        start = max(FLOOR, now - timedelta(days=span))
    found: list[tuple[int, int]] = []
    cursor = start
    while cursor < now:
        nxt = _add_months(cursor, months) if months else cursor + timedelta(days=days)
        stop = min(nxt, now)
        end = int(stop.timestamp()) if stop == now else int(stop.timestamp()) - 1
        begin = int(cursor.timestamp())
        if end >= begin:
            found.append((begin, end))
        cursor = nxt
    return found
