"""Normalisation of the sleep and activity streams against the database (#229): the rows and
their provenance, the ownership of each pass (a second writer's rows survive), two concurrent
passes, the purge race for every new stream, the job and the best-effort enqueue.

Setup rows are written through the ingest code as the owner; the pass runs as ``ownvitals_app``.
Synthetic users, nights, days and workouts only (``synthetic: true``); no real account.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobcore import JobsEnv, jenv_fixture  # noqa: F401 - fixtures
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.connections.purge import purge_imported
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.db.queue_locks import NORMALISATION
from ownvitals.derived.normalize_db import (
    ACTIVITY,
    MEASURES,
    SLEEP,
    SOURCES,
    WORKOUTS,
    Outcome,
    Source,
    normalise_all,
    normalise_owner,
)
from ownvitals.derived.normalize_job import KIND, TRIGGERS, enqueue_normalize
from ownvitals.derived.normalize_streams import ALGO_VERSION
from ownvitals.ingest.revisions import current
from ownvitals.ingest.rows import upsert_activity, upsert_sleep_summary, upsert_workout
from ownvitals.jobs import KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.withings.models import Activity, SleepSummary, Workout
from ownvitals.withings.streams import STREAMS
from test_derived_normalize_db import NOW, WEIGHT, Env

pytestmark = pytest.mark.usefixtures("jenv")

BED = 1_792_446_900  # 2026-10-19 21:55 UTC
WAKE = 1_792_474_800  # 2026-10-20 05:40 UTC
RUN = 1_792_501_200  # 2026-10-20 13:00 UTC


def summary(sid: int = 9001, start: int = BED, end: int = WAKE, **data: Any) -> SleepSummary:
    """A synthetic sleep summary (``synthetic: true``)."""
    body = data or {"total_sleep_time": 25_800, "hr_average": 54, "apnea_hypopnea_index": 3}
    return SleepSummary.model_validate(
        {
            "id": sid,
            "startdate": start,
            "enddate": end,
            "timezone": "Europe/Stockholm",
            "data": body,
        }
    )


def activity(on: str = "2026-10-20", **fields: Any) -> Activity:
    body = fields or {"steps": 8_412, "moderate": 1_260, "intense": 300}
    return Activity.model_validate({"date": on, "timezone": "Europe/Stockholm", **body})


def workout(wid: int = 501, **data: Any) -> Workout:
    return Workout.model_validate(
        {
            "id": wid,
            "category": 2,
            "startdate": RUN,
            "enddate": RUN + 1_800,
            "timezone": "Europe/Stockholm",
            "data": data or {"calories": 312, "steps": 4_100},
        }
    )


class Streams(Env):
    async def sleep(self, ctx: UserContext, connection: uuid.UUID, *rows: SleepSummary) -> None:
        async with user_session(self.factory, ctx) as db:
            for row in rows:
                await upsert_sleep_summary(db, ctx.user_id, connection, row, "UTC", NOW)

    async def days(self, ctx: UserContext, connection: uuid.UUID, *rows: Activity) -> None:
        async with user_session(self.factory, ctx) as db:
            for row in rows:
                await upsert_activity(db, ctx.user_id, connection, row, "UTC", NOW)

    async def workouts(self, ctx: UserContext, connection: uuid.UUID, *rows: Workout) -> None:
        async with user_session(self.factory, ctx) as db:
            for row in rows:
                await upsert_workout(db, ctx.user_id, connection, row, "UTC", NOW)

    async def feed(self, source: Source, ctx: UserContext, connection: uuid.UUID) -> None:
        """One row of ``source``'s stream (or one more, when called again)."""
        if source is SLEEP:
            await self.sleep(ctx, connection, summary())
        elif source is ACTIVITY:
            await self.days(ctx, connection, activity())
        elif source is WORKOUTS:
            await self.workouts(ctx, connection, workout())
        else:
            await self.ingest(ctx, connection, WEIGHT)

    async def pass_(self, ctx: UserContext, source: Source, **kw: Any) -> Outcome:
        return await normalise_owner(self.opener(ctx), ctx.user_id, source=source, now=NOW, **kw)

    async def keyed(self, ctx: UserContext) -> dict[str, Any]:
        return {r.metric_key: r for r in await self.rows(ctx)}


@pytest.fixture
def streams(jenv: JobsEnv) -> Streams:
    return Streams(jenv)


STREAM_SOURCES = pytest.mark.parametrize(
    "source", [SLEEP, ACTIVITY, WORKOUTS], ids=lambda s: s.stream
)


async def test_each_stream_writes_its_rows_with_their_provenance(streams: Streams) -> None:
    ctx, connection = streams.owner()
    await streams.sleep(ctx, connection, summary())
    await streams.days(ctx, connection, activity())
    await streams.workouts(ctx, connection, workout())
    outcomes = await normalise_all(streams.opener(ctx), ctx.user_id, now=NOW)
    assert {k: (o.status, o.inserted) for k, o in outcomes.items()} == {
        "measures": ("nothing", 0),
        "sleep_summary": ("written", 3),
        "activity_daily": ("written", 3),
        "workouts": ("written", 2),
    }
    rows = await streams.keyed(ctx)
    assert sorted(rows) == [
        "activity_intense", "activity_moderate", "activity_steps",
        "sleep_apnea_hypopnea_index", "sleep_hr_average", "sleep_total_sleep_time",
        "workout_calories", "workout_steps",
    ]  # fmt: skip
    hr = rows["sleep_hr_average"]
    assert (str(hr.value), hr.source_value_raw, hr.source_unit, hr.conversion) == (
        "54", 54, 0, "e0:unknown",
    )  # fmt: skip
    assert hr.ts == datetime.fromtimestamp(WAKE, UTC)  # the wake-up
    assert (hr.source_grpid, hr.source_type, hr.source_position) == (9001, None, None)
    steps = rows["activity_steps"]
    assert steps.ts == datetime(2026, 10, 20, 10, 0, tzinfo=UTC)  # noon in Stockholm (CEST)
    assert (steps.source_tz, steps.source_grpid) == ("Europe/Stockholm", None)
    assert rows["workout_steps"].source_grpid == 501
    assert rows["workout_steps"].ts == datetime.fromtimestamp(RUN, UTC)
    async with user_session(streams.factory, ctx) as db:
        revisions = {s.stream: await current(db, ctx.user_id, s.stream) for s in SOURCES}
    for row in rows.values():
        assert row.algo_version == ALGO_VERSION
        stream = {"sleep": "sleep_summary", "activity": "activity_daily", "workout": "workouts"}[
            row.metric_key.split("_")[0]
        ]
        assert row.input_revision == revisions[stream]
        assert len(bytes(row.input_fingerprint)) == 32
    again = await normalise_all(streams.opener(ctx), ctx.user_id, now=NOW)
    assert {o.status for k, o in again.items() if k != "measures"} == {"current"}
    assert await streams.keyed(ctx) == rows  # identical ids, values and provenance


async def test_a_changed_and_a_replaced_night_update_and_delete(streams: Streams) -> None:
    ctx, connection = streams.owner()
    await streams.sleep(ctx, connection, summary())
    await streams.pass_(ctx, SLEEP)
    # The same day again with a new value: updated in place.
    await streams.sleep(
        ctx, connection, summary(total_sleep_time=25_800, hr_average=57, apnea_hypopnea_index=3)
    )
    out = await streams.pass_(ctx, SLEEP)
    assert (out.status, out.inserted, out.updated, out.deleted) == ("written", 0, 1, 0)
    assert str((await streams.keyed(ctx))["sleep_hr_average"].value) == "57"
    # The same day replaced by another summary (new id, one field fewer): old rows go.
    await streams.sleep(ctx, connection, summary(sid=9002, total_sleep_time=25_000))
    out = await streams.pass_(ctx, SLEEP)
    assert (out.inserted, out.deleted) == (1, 3)
    assert {(r.metric_key, r.source_grpid) for r in await streams.rows(ctx)} == {
        ("sleep_total_sleep_time", 9002)
    }


async def test_a_second_writers_rows_survive_every_pass(streams: Streams) -> None:
    """#227 diffed and re-stamped every observation of the owner; each pass now owns only its
    part. The measures rows, the stream rows and a foreign writer's row all survive the others'
    passes, with their ids, values and provenance unchanged."""
    ctx, connection = streams.owner()
    await streams.ingest(ctx, connection, WEIGHT)
    await streams.sleep(ctx, connection, summary())
    await streams.days(ctx, connection, activity())
    await normalise_all(streams.opener(ctx), ctx.user_id, now=NOW)
    # A second writer: a row no pass produces, with its own provenance (synthetic).
    streams.jenv.sql(
        "INSERT INTO observations (owner_id, metric_key, ts, source_ts, source_tz, value, "
        "source_value_raw, source_unit, conversion, algo_version, input_revision, "
        "input_fingerprint) VALUES (:o, 'other_writer', :ts, :ts, 'UTC', 1, 1, 0, 'e0:unknown', "
        "'other/1', 7, :f)",
        o=ctx.user_id,
        ts=datetime(2026, 10, 20, 12, tzinfo=UTC),
        f=b"\x07" * 32,
    )
    before = await streams.rows(ctx)
    # Force every pass to recompute and write (as after a change of its inputs).
    streams.jenv.sql(
        "UPDATE observations SET input_revision = 0 WHERE owner_id = :o "
        "AND metric_key <> 'other_writer'",
        o=ctx.user_id,
    )
    for source in SOURCES:
        await streams.pass_(ctx, source)
    after = await streams.rows(ctx)
    assert after == before
    # A changed night: only the sleep rows move to the new revision.
    await streams.sleep(ctx, connection, summary(sid=9003, hr_average=50))
    out = await streams.pass_(ctx, SLEEP)
    assert (out.deleted, out.inserted) == (3, 1)
    rest = {
        r.metric_key: r for r in await streams.rows(ctx) if not r.metric_key.startswith("sleep")
    }
    assert rest == {r.metric_key: r for r in before if not r.metric_key.startswith("sleep")}


async def test_a_computed_row_outside_the_pass_stops_it(
    streams: Streams, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, connection = streams.owner()
    await streams.sleep(ctx, connection, summary())
    rogue = Source(
        stream=SLEEP.stream,
        algo_version=SLEEP.algo_version,
        prefix=SLEEP.prefix,
        claims=lambda row: False,
        inputs=SLEEP.inputs,
        digest=SLEEP.digest,
        load=SLEEP.load,
        compute=SLEEP.compute,
    )
    with pytest.raises(RuntimeError, match="outside the rows it owns"):
        await streams.pass_(ctx, rogue)
    assert await streams.rows(ctx) == []


@pytest.mark.parametrize("source", [MEASURES, SLEEP], ids=lambda s: s.stream)
async def test_two_concurrent_passes_both_end_without_error(
    streams: Streams, source: Source
) -> None:
    """The review of #227: both passes read the same inputs; without the owner's lock the second
    insert failed on ``observations_source_key``."""
    ctx, connection = streams.owner()
    await streams.feed(source, ctx, connection)
    arrived = 0
    both = asyncio.Event()

    async def barrier() -> None:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 10)

    outcomes = await asyncio.gather(
        streams.pass_(ctx, source, between=barrier),
        streams.pass_(ctx, source, between=barrier),
        return_exceptions=True,
    )
    assert all(isinstance(o, Outcome) for o in outcomes), outcomes
    statuses = sorted((o.status, o.inserted) for o in outcomes if isinstance(o, Outcome))
    inserted = max(n for _, n in statuses)
    assert inserted > 0
    assert statuses == [("written", 0), ("written", inserted)]
    assert len(await streams.rows(ctx)) == inserted


async def test_the_write_waits_for_the_owners_lock(streams: Streams) -> None:
    """A pass's write cannot start while another transaction holds the owner's lock."""
    ctx, connection = streams.owner()
    await streams.sleep(ctx, connection, summary())
    holder: list[asyncio.Task[Outcome]] = []
    waited: list[bool] = []

    async def hold_then_release() -> None:
        async with user_session(streams.factory, ctx) as db:
            await db.execute(
                text("SELECT pg_advisory_xact_lock(:ns, hashtext(CAST(:o AS text)))"),
                {"ns": NORMALISATION, "o": ctx.user_id},
            )
            holder.append(asyncio.ensure_future(streams.pass_(ctx, SLEEP)))
            await asyncio.sleep(0.5)
            waited.append(not holder[0].done())

    await hold_then_release()
    outcome = await holder[0]
    assert waited == [True]
    assert outcome.status == "written"


@STREAM_SOURCES
async def test_a_purge_between_the_read_and_the_write_writes_nothing(
    streams: Streams, source: Source
) -> None:
    """Issue #187's comment, for each new stream."""
    ctx, connection = streams.owner()
    await streams.feed(source, ctx, connection)

    async def purge() -> None:
        await purge_imported(streams.factory, ctx)

    assert (await streams.pass_(ctx, source, between=purge)).status == "stale"
    assert await streams.rows(ctx) == []
    assert (await streams.pass_(ctx, source)).status == "nothing"
    assert await streams.rows(ctx) == []


@STREAM_SOURCES
async def test_a_purge_and_the_same_data_again_is_still_stale(
    streams: Streams, source: Source
) -> None:
    ctx, connection = streams.owner()
    await streams.feed(source, ctx, connection)

    async def purge_and_feed_again() -> None:
        await purge_imported(streams.factory, ctx)
        await streams.feed(source, ctx, connection)

    assert (await streams.pass_(ctx, source, between=purge_and_feed_again)).status == "stale"
    assert await streams.rows(ctx) == []
    assert (await streams.pass_(ctx, source)).status == "written"


@STREAM_SOURCES
async def test_a_moved_counter_alone_is_stale(streams: Streams, source: Source) -> None:
    ctx, connection = streams.owner()
    await streams.feed(source, ctx, connection)

    async def bump_only() -> None:
        streams.jenv.sql(
            "UPDATE data_revisions SET revision = revision + 1 WHERE owner_id = :o AND stream = :s",
            o=ctx.user_id,
            s=source.stream,
        )

    assert (await streams.pass_(ctx, source, between=bump_only)).status == "stale"
    assert await streams.rows(ctx) == []


@STREAM_SOURCES
async def test_a_changed_row_with_the_same_counter_is_stale(
    streams: Streams, source: Source
) -> None:
    """The fingerprint is checked on its own, not only through the counter."""
    ctx, connection = streams.owner()
    await streams.feed(source, ctx, connection)
    statement = {
        "sleep_summary": "UPDATE sleep_summaries SET payload_hash = :h WHERE owner_id = :o",
        "activity_daily": "UPDATE activity_daily SET payload_hash = :h WHERE owner_id = :o",
        "workouts": "UPDATE workouts SET payload_hash = :h WHERE owner_id = :o",
    }[source.stream]

    async def rewrite_hash() -> None:
        streams.jenv.sql(statement, h=b"\x09" * 32, o=ctx.user_id)

    assert (await streams.pass_(ctx, source, between=rewrite_hash)).status == "stale"
    assert await streams.rows(ctx) == []


@STREAM_SOURCES
async def test_the_write_holds_the_revision_row_so_a_purge_waits_for_it(
    streams: Streams, source: Source
) -> None:
    ctx, connection = streams.owner()
    await streams.feed(source, ctx, connection)
    state = {"writing": False}
    blocked: list[bool] = []
    purge_task: list[asyncio.Task[None]] = []

    async def mark() -> None:
        state["writing"] = True

    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with user_session(streams.factory, ctx) as db:
            yield db
            if state["writing"]:
                purge_task.append(asyncio.ensure_future(purge_imported(streams.factory, ctx)))
                await asyncio.sleep(0.5)
                blocked.append(not purge_task[0].done())

    outcome = await normalise_owner(open_tx, ctx.user_id, source=source, between=mark, now=NOW)
    await purge_task[0]
    assert outcome.status == "written"
    assert blocked == [True]
    assert await streams.rows(ctx) == []  # the purge ran after the commit


async def test_only_the_owners_stream_rows_are_read_and_written(streams: Streams) -> None:
    mine, my_connection = streams.owner()
    other, other_connection = streams.owner()
    await streams.sleep(mine, my_connection, summary())
    await streams.sleep(other, other_connection, summary(sid=4))
    await streams.pass_(mine, SLEEP)
    assert {r.source_grpid for r in await streams.rows(mine)} == {9001}
    assert await streams.rows(other) == []


async def test_the_job_normalises_every_stream_in_the_workers_context(streams: Streams) -> None:
    from ownvitals.derived.normalize_job import register_normalize_kind

    jenv = streams.jenv
    mine, my_connection = streams.owner()
    other, other_connection = streams.owner()
    await streams.ingest(mine, my_connection, WEIGHT)
    await streams.sleep(mine, my_connection, summary())
    await streams.days(mine, my_connection, activity())
    await streams.workouts(other, other_connection, workout())
    registry = KindRegistry()
    register_normalize_kind(registry)
    payload = json.dumps({"connection_id": str(my_connection), "origin": "backfill"})
    job_id = jenv.job(mine.user_id, kind=KIND, input=payload)[1]
    claimed = await queue.claim(streams.factory, "w", 60, [KIND])
    assert claimed is not None
    execution = Execution(claimed)
    config = RunConfig(lease_seconds=60, accounts=jenv.accounts)
    await execute(execution, streams.factory, registry, config, asyncio.Event())
    assert (execution.result, jenv.row(job_id).state) == ("completed", "completed")
    prefixes = {r.metric_key.split("_")[0] for r in await streams.rows(mine)}
    assert {"sleep", "activity", "weight"} <= prefixes
    assert "workout" not in prefixes
    assert await streams.rows(other) == []


def test_every_normalised_stream_is_an_ingest_stream_that_triggers_a_pass() -> None:
    names = {stream.name for stream in STREAMS}
    triggers = set(TRIGGERS)
    assert triggers == {"measures", "sleep_summary", "activity_daily", "workouts"}
    assert names >= triggers
    # Not normalised (yet): the ECG has its own pipeline; the two high-volume series and the
    # devices and goals are not readings the rules or scores read (docs/dev/derived.md).
    assert names - triggers == {"ecg", "sleep_series", "intraday", "devices", "goals"}


async def test_the_enqueue_is_best_effort(
    streams: Streams, caplog: pytest.LogCaptureFixture
) -> None:
    """A connection disconnected while the job ran: the ingest has committed, so the refused
    enqueue is logged (by class name, no id) and the job does not fail."""
    ctx, connection = streams.owner()
    streams.jenv.sql("UPDATE connections SET status = 'disconnected' WHERE id = :id", id=connection)
    with caplog.at_level(logging.WARNING, logger="ownvitals.derived.normalize_job"):
        await enqueue_normalize(streams.factory, connection, "live")
    assert (
        streams.jenv.sql(
            "SELECT count(*) FROM jobs WHERE owner_id = :o", o=ctx.user_id
        ).scalar_one()
        == 0
    )
    (record,) = [r for r in caplog.records if r.name == "ownvitals.derived.normalize_job"]
    assert record.getMessage() == "normalisation not queued"
    assert record.__dict__["error_type"] in {"DBAPIError", "DataError", "ProgrammingError"}
    assert str(connection) not in record.getMessage() + repr(record.__dict__)


async def test_an_unexpected_enqueue_error_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort covers database errors only; a bug elsewhere still fails loudly."""
    from ownvitals.jobs import queue as queue_module

    async def broken(*args: object, **kwargs: object) -> None:
        raise ValueError("a bug")

    monkeypatch.setattr(queue_module, "enqueue", broken)
    with pytest.raises(ValueError, match="a bug"):
        await enqueue_normalize("factory", uuid.uuid4(), "live")  # type: ignore[arg-type]
