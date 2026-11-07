"""Fetch from a stored cursor to now (the incremental sync): dated shapes and the day-by-day
series walk.

Split from ``ingest.fetch`` (no behaviour change).
"""

from datetime import datetime

from ownvitals.ingest.calls import Account, Classified, Got
from ownvitals.ingest.fetch import series_window
from ownvitals.ingest.fetch_dated import dated_window
from ownvitals.ingest.when import FLOOR
from ownvitals.jobs.run import JobRun
from ownvitals.withings.client import WithingsClient
from ownvitals.withings.errors import RetryBudget

_DAY = 86_400


def _spans(start: int, end: int) -> list[tuple[int, int]]:
    """Inclusive day spans from ``start`` up to ``end``, with no gap and no shared second."""
    spans: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + _DAY, end)
        stop = nxt if nxt == end else nxt - 1
        if stop >= cursor:
            spans.append((cursor, stop))
        cursor = nxt if nxt == end else cursor + _DAY
    return spans


async def series_since(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    lane: int,
    budget: RetryBudget,
    table: str,
    lastupdate: int | None,
    end: int,
    now: datetime,
    *,
    history_days: int,
) -> Got | Classified:
    """Walk from the stored cursor to ``end``, one day at a time (the calls allow 24 h).

    A missing cursor covers ``history_days``, not the last day only. The cursor moves to the
    end only after every day in the gap has been requested.
    """
    floor = int(FLOOR.timestamp())
    start = max(floor, end - history_days * _DAY) if lastupdate is None else max(floor, lastupdate)
    if start >= end:
        return Got(lastupdate=start, empty=True, zone=account.zone)
    skipped = 0
    empty = True
    zone = account.zone
    covered = start
    for begin, stop in _spans(start, end):
        await job.checkpoint()
        got = await series_window(job, account, client, lane, budget, table, begin, stop, now)
        if isinstance(got, Classified):
            return got
        skipped += got.skipped
        empty = empty and got.empty
        zone = got.zone or zone
        covered = stop
    return Got(skipped=skipped, zone=zone, lastupdate=covered, empty=empty)


async def incremental(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    lane: int,
    budget: RetryBudget,
    shape: str,
    lastupdate: int | None,
    end: int,
    now: datetime,
    *,
    history_days: int | None,
) -> Got | Classified:
    """Activity, workouts and sleep summary use ``lastupdate``. Series walk forward from it."""
    if shape in {"activity", "workouts", "sleep_summary"}:
        return await dated_window(
            job,
            account,
            client,
            lane,
            budget,
            shape,
            0,
            end,
            now,
            lastupdate=0 if lastupdate is None else lastupdate,
        )
    table = "sleep_series" if shape == "sleep_series" else "intraday"
    return await series_since(
        job,
        account,
        client,
        lane,
        budget,
        table,
        lastupdate,
        end,
        now,
        history_days=history_days or 1,
    )
