"""Fetch one dated backfill window (activity, workouts, sleep summary) and write it.

Split from ``ingest.fetch`` (no behaviour change). Partition months commit before their rows.
"""

from datetime import date, datetime
from typing import Any

from ownvitals.ingest.calls import Account, Classified, Got, limited
from ownvitals.ingest.rows import (
    upsert_activity,
    upsert_sleep_summary,
    upsert_workout,
)
from ownvitals.ingest.when import from_epoch
from ownvitals.jobs.run import JobRun
from ownvitals.withings.client import WithingsClient
from ownvitals.withings.errors import RetryBudget


def next_offset(more: bool, offset: int) -> int | None:
    return offset if more else None


def _writer(shape: str) -> Any:
    if shape == "activity":
        return upsert_activity
    if shape == "workouts":
        return upsert_workout
    return upsert_sleep_summary


async def dated_window(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    lane: int,
    budget: RetryBudget,
    shape: str,
    start: int,
    end: int,
    now: datetime,
    *,
    lastupdate: int | None = None,
) -> Got | Classified:
    """A date window, or, when ``lastupdate`` is set, every row modified after that cursor.

    Backfill passes no cursor, so the date bounds stay the window it was given. The returned
    cursor is the end of the range the caller has now covered.
    """
    if lastupdate is None:
        start_day = from_epoch(start).date()
        end_day = from_epoch(end).date()
    else:
        start_day = None
        end_day = None
    offset = 0
    skipped = 0
    empty = True
    zone = account.zone
    writer = _writer(shape)
    while True:
        body = await _dated_page(
            job, account, client, lane, budget, shape, start_day, end_day, offset, lastupdate
        )
        if isinstance(body, Classified):
            return body
        rows = body.activities if shape == "activity" else body.series
        if rows:
            empty = False
        async with job.fenced() as fs:
            for row in rows:
                zone = getattr(row, "timezone", None) or zone
                outcome = await writer(
                    fs.db, job.ctx.user_id, account.connection_id, row, zone, now
                )
                if outcome == "skipped":
                    skipped += 1
        nxt = next_offset(bool(body.more), int(body.offset))
        if nxt is None or nxt <= offset:
            break
        offset = nxt
        await job.checkpoint()
    return Got(skipped=skipped, zone=zone, lastupdate=end, empty=empty)


async def _dated_page(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    lane: int,
    budget: RetryBudget,
    shape: str,
    start_day: date | None,
    end_day: date | None,
    offset: int,
    lastupdate: int | None,
) -> Any:
    if shape == "activity":
        return await limited(
            job,
            account,
            lane,
            lambda offset=offset: client.getactivity(
                account.access_token,
                startdateymd=start_day,
                enddateymd=end_day,
                lastupdate=lastupdate,
                offset=offset or None,
                budget=budget,
            ),
        )
    if shape == "workouts":
        return await limited(
            job,
            account,
            lane,
            lambda offset=offset: client.getworkouts(
                account.access_token,
                startdateymd=start_day,
                enddateymd=end_day,
                lastupdate=lastupdate,
                offset=offset or None,
                budget=budget,
            ),
        )
    return await limited(
        job,
        account,
        lane,
        lambda offset=offset: client.sleep_getsummary(
            account.access_token,
            startdateymd=start_day,
            enddateymd=end_day,
            lastupdate=lastupdate,
            offset=offset or None,
            budget=budget,
        ),
    )
