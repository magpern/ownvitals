"""Fetch one backfill window and write it. Partition months commit before their rows."""

from datetime import date, datetime
from typing import Any

from ownvitals.ingest.calls import Account, Classified, Got, limited
from ownvitals.ingest.partitions import ensure_month
from ownvitals.ingest.rows import (
    goal_body,
    upsert_activity,
    upsert_device,
    upsert_ecg,
    upsert_measure_group,
    upsert_sleep_summary,
    upsert_workout,
)
from ownvitals.ingest.series import intraday_points, sleep_points, write_points
from ownvitals.ingest.when import FLOOR, from_epoch, month_of
from ownvitals.jobs.run import JobRun
from ownvitals.withings.client import WithingsClient
from ownvitals.withings.errors import RetryBudget
from ownvitals.withings.models import HeartListEntry


def _next(more: bool, offset: int) -> int | None:
    return offset if more else None


async def measure_window(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    lane: int,
    budget: RetryBudget,
    start: int,
    end: int,
    now: datetime,
    *,
    lastupdate: int | None = None,
) -> Got | Classified:
    skipped = 0
    empty = True
    zone = account.zone
    seen: int | None = None
    ranged = lastupdate is None
    for category in (1, 2):
        offset = 0
        while True:
            body = await limited(
                job,
                account,
                lane,
                lambda offset=offset, category=category: client.getmeas(
                    account.access_token,
                    category=category,
                    startdate=start if ranged else None,
                    enddate=end if ranged else None,
                    lastupdate=lastupdate,
                    offset=offset or None,
                    budget=budget,
                ),
            )
            if isinstance(body, Classified):
                return body
            zone = body.timezone or zone
            if body.updatetime is not None:
                seen = int(body.updatetime)
            if body.measuregrps:
                empty = False
            async with job.fenced() as fs:
                for group in body.measuregrps:
                    outcome = await upsert_measure_group(
                        fs.db, job.ctx.user_id, account.connection_id, group, zone, now
                    )
                    if outcome == "skipped":
                        skipped += 1
            nxt = _next(bool(body.more), int(body.offset))
            if nxt is None or nxt <= offset:
                break
            offset = nxt
            await job.checkpoint()
    return Got(skipped=skipped, zone=zone, lastupdate=seen, empty=empty)


async def ecg_window(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    lane: int,
    budget: RetryBudget,
    start: int,
    end: int,
    now: datetime,
) -> Got | Classified:
    entries: list[HeartListEntry] = []
    offset = 0
    zone = account.zone
    while True:
        body = await limited(
            job,
            account,
            lane,
            lambda offset=offset: client.heart_list(
                account.access_token,
                startdate=start,
                enddate=end,
                offset=offset or None,
                budget=budget,
            ),
        )
        if isinstance(body, Classified):
            return body
        entries.extend(body.series)
        nxt = _next(bool(body.more), int(body.offset))
        if nxt is None or nxt <= offset:
            break
        offset = nxt
        await job.checkpoint()
    skipped = 0
    for entry in entries:
        if entry.ecg is None:
            continue
        signal_id = entry.ecg.signalid
        zone = entry.timezone or zone
        signal = await limited(
            job,
            account,
            lane,
            lambda signal_id=signal_id: client.heart_get(
                account.access_token, signal_id, budget=budget
            ),
        )
        if isinstance(signal, Classified):
            return signal
        async with job.fenced() as fs:
            outcome = await upsert_ecg(
                fs.db, job.ctx.user_id, account.connection_id, entry, signal, zone, now
            )
            if outcome == "skipped":
                skipped += 1
    return Got(skipped=skipped, zone=zone, lastupdate=end, empty=not any(e.ecg for e in entries))


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
        nxt = _next(bool(body.more), int(body.offset))
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


async def series_window(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    lane: int,
    budget: RetryBudget,
    table: str,
    start: int,
    end: int,
    now: datetime,
) -> Got | Classified:
    if table == "sleep_series":
        body = await limited(
            job,
            account,
            lane,
            lambda: client.sleep_get(
                account.access_token, startdate=start, enddate=end, budget=budget
            ),
        )
        points: list[Any] = []
        skipped = 0
        if not isinstance(body, Classified):
            points, skipped = sleep_points(body, account.zone, now)
    else:
        body = await limited(
            job,
            account,
            lane,
            lambda: client.getintradayactivity(
                account.access_token, startdate=start, enddate=end, budget=budget
            ),
        )
        points = []
        skipped = 0
        if not isinstance(body, Classified):
            points, skipped = intraday_points(body, account.zone, now)
    if isinstance(body, Classified):
        return body
    for month in sorted({month_of(point.ts) for point in points}):
        async with job.fenced() as fs:
            await ensure_month(fs.db, table, month)
    if points:
        async with job.fenced() as fs:
            await write_points(fs.db, table, job.ctx.user_id, account.connection_id, list(points))
    return Got(skipped=skipped, empty=not points, zone=account.zone, lastupdate=end)


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


async def once(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    lane: int,
    budget: RetryBudget,
    shape: str,
    now: datetime,
) -> Got | Classified:
    if shape == "devices":
        body = await limited(
            job, account, lane, lambda: client.getdevice(account.access_token, budget=budget)
        )
        if isinstance(body, Classified):
            return body
        skipped = 0
        async with job.fenced() as fs:
            for device in body.devices:
                outcome = await upsert_device(
                    fs.db, job.ctx.user_id, account.connection_id, device, account.zone, now
                )
                if outcome == "skipped":
                    skipped += 1
        return Got(
            skipped=skipped,
            empty=not body.devices,
            no_devices=not body.devices,
            zone=account.zone,
        )
    body = await limited(
        job, account, lane, lambda: client.getgoals(account.access_token, budget=budget)
    )
    if isinstance(body, Classified):
        return body
    return Got(empty=False, goals=goal_body(body.goals), zone=account.zone)


async def window(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    lane: int,
    budget: RetryBudget,
    shape: str,
    start: int,
    end: int,
    now: datetime,
) -> Got | Classified:
    if shape == "measures":
        return await measure_window(job, account, client, lane, budget, start, end, now)
    if shape == "ecg":
        return await ecg_window(job, account, client, lane, budget, start, end, now)
    if shape in {"activity", "workouts", "sleep_summary"}:
        return await dated_window(job, account, client, lane, budget, shape, start, end, now)
    if shape == "sleep_series":
        return await series_window(
            job, account, client, lane, budget, "sleep_series", start, end, now
        )
    if shape == "intraday":
        return await series_window(job, account, client, lane, budget, "intraday", start, end, now)
    return await once(job, account, client, lane, budget, shape, now)
