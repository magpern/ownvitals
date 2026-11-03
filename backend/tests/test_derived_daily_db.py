"""The daily pass against the database (#186): rows and provenance, idempotence, soft deletes
and backdating marking exactly the affected days, the purge race, concurrent passes, the rows of
other metric keys, the worker context and the chain from the normaliser.

Setup rows go through the ingest code and the normaliser; the daily pass runs as
``ownvitals_app``. Synthetic users, groups and measures only; no real account or measurement.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobcore import JobsEnv, jenv_fixture  # noqa: F401 - fixtures
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.connections.purge import purge_imported
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.derived.daily import ALGO_VERSION
from ownvitals.derived.daily_db import Outcome, daily_owner
from ownvitals.derived.daily_job import (
    KIND,
    DailyInput,
    enqueue_daily,
    register_daily_kind,
)
from ownvitals.derived.normalize import ALGO_VERSION as NORMALIZE_VERSION
from ownvitals.derived.normalize_db import normalise_owner
from ownvitals.derived.normalize_job import KIND as NORMALIZE_KIND
from ownvitals.derived.normalize_job import register_normalize_kind
from ownvitals.ingest.revisions import current
from ownvitals.ingest.rows import soft_delete_measure
from ownvitals.jobs import KINDS, KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.worker.kinds import worker_registry
from test_derived_normalize_db import Env as NormalizeEnv
from test_derived_normalize_db import group

pytestmark = pytest.mark.usefixtures("jenv")

NOW = datetime(2026, 11, 1, tzinfo=UTC)
OCT_20 = 1_792_477_800  # 2026-10-20 06:30 UTC = 08:30 in Stockholm
DAY = 86_400
SNAPSHOT = (
    "SELECT id, metric_key, day, method, value, morning, mean, min, max, mad, n, source_groups, "
    "algo_version, input_revision, input_fingerprint FROM metric_daily WHERE owner_id = :o "
    "ORDER BY metric_key, day"
)


def weight(grpid: int, when: int, value: int, **extra: Any) -> Any:
    return group(grpid, when, (1, value, -3, None), **extra)


class Env(NormalizeEnv):
    async def normalise(self, ctx: UserContext) -> None:
        await normalise_owner(self.opener(ctx), ctx.user_id, now=NOW)

    async def daily(self, ctx: UserContext, **kw: Any) -> Outcome:
        return await daily_owner(self.opener(ctx), ctx.user_id, **kw)

    async def both(self, ctx: UserContext) -> Outcome:
        await self.normalise(ctx)
        return await self.daily(ctx)

    async def days(self, ctx: UserContext) -> list[Any]:
        async with user_session(self.factory, ctx) as db:
            return list((await db.execute(text(SNAPSHOT), {"o": ctx.user_id})).all())


@pytest.fixture
def env(jenv: JobsEnv) -> Env:
    return Env(jenv)


async def seeded(env: Env) -> tuple[UserContext, uuid.UUID]:
    ctx, connection = env.owner()
    await env.ingest(
        ctx,
        connection,
        weight(1, OCT_20, 80_050),  # morning, 20th
        weight(2, OCT_20 + 7 * 3600, 81_000),  # afternoon, same day
        weight(3, OCT_20 + DAY, 79_500),  # morning, 21st
        weight(4, OCT_20 + 2 * DAY + 8 * 3600, 79_000),  # 14:30 local, 22nd: no morning
        group(5, OCT_20, (11, 61, 0, None)),  # a heart pulse
        group(6, OCT_20, (4242, 17, 0, None)),  # a type the analysis does not judge
    )
    return ctx, connection


def key(row: Any) -> tuple[str, date]:
    return (row.metric_key, row.day)


async def test_a_pass_writes_the_daily_values_with_their_provenance(env: Env) -> None:
    ctx, connection = await seeded(env)
    outcome = await env.both(ctx)
    assert (outcome.status, outcome.inserted, outcome.updated, outcome.deleted) == (
        "written", 4, 0, 0,
    )  # fmt: skip
    rows = await env.days(ctx)
    assert [key(r) for r in rows] == [
        ("heart_pulse", date(2026, 10, 20)),
        ("weight", date(2026, 10, 20)),
        ("weight", date(2026, 10, 21)),
        ("weight", date(2026, 10, 22)),
    ]
    first = rows[1]
    assert (first.method, str(first.value), str(first.morning), first.n) == (
        "morning", "80.050", "80.050", 2,
    )  # fmt: skip
    assert first.source_groups == [1]
    assert (str(first.min), str(first.max)) == ("80.050", "81.000")
    assert str(first.mean) == "80.525"
    assert rows[3].method == "median"
    assert rows[3].morning is None
    assert rows[3].source_groups == [4]
    async with user_session(env.factory, ctx) as db:
        revision = await current(db, ctx.user_id, "measures")
    assert {(r.algo_version, r.input_revision) for r in rows} == {(ALGO_VERSION, revision)}
    assert len({bytes(r.input_fingerprint) for r in rows}) == 1
    assert len(bytes(rows[0].input_fingerprint)) == 32
    assert connection  # the owner has one connection; the pass covers the owner


async def test_dst_trips_and_midnight_land_on_the_right_local_day(env: Env) -> None:
    ctx, connection = env.owner()
    spring = 1_774_742_400  # 2026-03-29 00:00 UTC = 01:00 CET, the night of the change
    await env.ingest(
        ctx,
        connection,
        weight(1, spring + 3600, 80_000),  # 03:00 CEST on the 29th
        weight(2, spring + 2 * 3600, 81_000),  # 04:00 CEST: morning
        weight(3, OCT_20 + 15 * 3600 + 29 * 60 + 59, 82_000),  # 23:59:59 on the 20th, Stockholm
        weight(4, OCT_20 + 15 * 3600 + 30 * 60, 83_000, timezone="America/New_York"),  # 18:00 NYC
        weight(5, OCT_20 + 15 * 3600 + 30 * 60 + 59, 84_000),  # 00:00:59 on the 21st, Stockholm
    )
    await env.both(ctx)
    rows = {key(r): r for r in await env.days(ctx)}
    march = rows["weight", date(2026, 3, 29)]
    assert (march.method, march.source_groups, march.n) == ("morning", [2], 2)
    # Group 3 is the 20th in Stockholm and group 4 is the 20th in New York: one day, no morning.
    twentieth = rows["weight", date(2026, 10, 20)]
    assert (twentieth.method, twentieth.source_groups, str(twentieth.value)) == (
        "median", [3, 4], "82.500",
    )  # fmt: skip
    assert rows["weight", date(2026, 10, 21)].source_groups == [5]
    assert len(rows) == 3


async def test_a_second_run_changes_nothing_and_a_recompute_is_identical(env: Env) -> None:
    ctx, _ = await seeded(env)
    assert (await env.both(ctx)).status == "written"
    first = await env.days(ctx)
    assert (await env.daily(ctx)).status == "current"
    assert await env.days(ctx) == first
    env.jenv.sql("UPDATE metric_daily SET input_revision = 0 WHERE owner_id = :o", o=ctx.user_id)
    again = await env.daily(ctx)
    assert (again.status, again.inserted, again.updated, again.deleted) == ("written", 0, 0, 0)
    assert again.affected == ()
    assert await env.days(ctx) == first  # same ids, same values, same provenance
    # A stored row that differs from the computed one is repaired, byte for byte.
    env.jenv.sql(
        "UPDATE metric_daily SET value = value + 1, morning = morning + 1, min = min + 1, "
        "max = max + 1, mean = mean + 1, input_revision = 0 WHERE owner_id = :o "
        "AND metric_key = 'weight' AND day = :d",
        o=ctx.user_id,
        d=date(2026, 10, 21),
    )
    repaired = await env.daily(ctx)
    assert repaired.affected == (("weight", date(2026, 10, 21)),)
    assert await env.days(ctx) == first


async def test_behind_until_the_normaliser_has_caught_up(env: Env) -> None:
    ctx, connection = await seeded(env)
    assert (await env.daily(ctx)).status == "behind"  # no observations yet
    await env.normalise(ctx)
    assert (await env.daily(ctx)).status == "written"
    await env.ingest(ctx, connection, weight(7, OCT_20 + 3 * DAY, 78_000))
    assert (await env.daily(ctx)).status == "behind"  # the source moved; observations did not
    assert len(await env.days(ctx)) == 4  # and nothing was touched
    assert (await env.both(ctx)).inserted == 1


async def test_nothing_imported_is_nothing(env: Env) -> None:
    ctx, _ = env.owner()
    assert (await env.daily(ctx)).status == "nothing"


async def test_a_soft_delete_marks_exactly_its_day_stale_and_recomputes_it(env: Env) -> None:
    ctx, connection = await seeded(env)
    await env.both(ctx)
    before = {key(r): r for r in await env.days(ctx)}
    async with user_session(env.factory, ctx) as db:
        assert await soft_delete_measure(db, ctx.user_id, connection, 3)  # the only 21st reading
    outcome = await env.both(ctx)
    assert (outcome.status, outcome.deleted, outcome.updated, outcome.inserted) == (
        "written", 1, 0, 0,
    )  # fmt: skip
    assert outcome.affected == (("weight", date(2026, 10, 21)),)
    after = {key(r): r for r in await env.days(ctx)}
    assert set(before) - set(after) == {("weight", date(2026, 10, 21))}
    for k in after:  # the untouched days keep their id and values; only provenance moved
        assert after[k].id == before[k].id
        assert after[k].value == before[k].value
        assert after[k].source_groups == before[k].source_groups
    # A soft delete of one of two readings of a day changes that day only (n, mean, spread).
    async with user_session(env.factory, ctx) as db:
        assert await soft_delete_measure(db, ctx.user_id, connection, 2)
    outcome = await env.both(ctx)
    assert outcome.affected == (("weight", date(2026, 10, 20)),)
    day = next(r for r in await env.days(ctx) if key(r) == ("weight", date(2026, 10, 20)))
    assert (day.n, str(day.mean)) == (1, "80.050")


async def test_a_backdated_edit_marks_the_old_and_the_new_day(env: Env) -> None:
    ctx, connection = await seeded(env)
    await env.both(ctx)
    # Group 4 (the 22nd) is re-sent with an instant on the 21st: the 22nd loses its only
    # reading, the 21st gains a second one.
    await env.ingest(ctx, connection, weight(4, OCT_20 + DAY + 8 * 3600, 79_000))
    outcome = await env.both(ctx)
    assert outcome.affected == (
        ("weight", date(2026, 10, 21)),
        ("weight", date(2026, 10, 22)),
    )
    assert (outcome.updated, outcome.deleted, outcome.inserted) == (1, 1, 0)
    day = next(r for r in await env.days(ctx) if key(r) == ("weight", date(2026, 10, 21)))
    assert (day.n, day.method, day.source_groups) == (2, "morning", [3])


async def test_a_purge_between_the_read_and_the_write_writes_nothing(env: Env) -> None:
    ctx, _ = await seeded(env)
    await env.normalise(ctx)

    async def purge() -> None:
        await purge_imported(env.factory, ctx)

    assert (await env.daily(ctx, between=purge)).status == "stale"
    assert await env.days(ctx) == []
    assert (await env.daily(ctx)).status == "nothing"
    assert await env.days(ctx) == []


async def test_a_purge_and_the_same_data_again_is_stale_until_it_is_normalised(env: Env) -> None:
    """The revision counter restarts at the old value; the observations are the check."""
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, weight(1, OCT_20, 80_050))
    await env.normalise(ctx)
    async with user_session(env.factory, ctx) as db:
        assert await current(db, ctx.user_id, "measures") == 1

    async def purge_and_ingest_again() -> None:
        await purge_imported(env.factory, ctx)
        await env.ingest(ctx, connection, weight(1, OCT_20, 80_050))

    assert (await env.daily(ctx, between=purge_and_ingest_again)).status == "stale"
    assert await env.days(ctx) == []
    assert (await env.daily(ctx)).status == "behind"  # the observations are gone: normalise first
    assert (await env.both(ctx)).status == "written"


async def test_the_same_data_normalised_again_is_the_same_input(env: Env) -> None:
    """Inputs are the observations' content: identical rows after a purge are a valid input."""
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, weight(1, OCT_20, 80_050))
    await env.normalise(ctx)

    async def purge_and_start_again() -> None:
        await purge_imported(env.factory, ctx)
        await env.ingest(ctx, connection, weight(1, OCT_20, 80_050))
        await env.normalise(ctx)

    assert (await env.daily(ctx, between=purge_and_start_again)).status == "written"
    assert [str(r.value) for r in await env.days(ctx)] == ["80.050"]


async def test_a_new_ingest_between_the_read_and_the_write_writes_nothing(env: Env) -> None:
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, weight(1, OCT_20, 80_050))
    await env.normalise(ctx)

    async def ingest_more() -> None:
        await env.ingest(ctx, connection, weight(2, OCT_20 + DAY, 79_000))

    assert (await env.daily(ctx, between=ingest_more)).status == "stale"
    assert await env.days(ctx) == []


async def test_a_moved_counter_alone_is_stale(env: Env) -> None:
    ctx, _ = await seeded(env)
    await env.normalise(ctx)

    async def bump_only() -> None:
        env.jenv.sql(
            "UPDATE data_revisions SET revision = revision + 1 WHERE owner_id = :o",
            o=ctx.user_id,
        )

    assert (await env.daily(ctx, between=bump_only)).status == "stale"
    assert await env.days(ctx) == []


async def test_changed_observations_alone_are_stale(env: Env) -> None:
    """The fingerprint is checked on its own, with the revision unchanged."""
    ctx, _ = await seeded(env)
    await env.normalise(ctx)

    async def edit_observation() -> None:
        env.jenv.sql(
            "UPDATE observations SET value = value + 1 WHERE owner_id = :o "
            "AND metric_key = 'weight' AND source_grpid = 1",
            o=ctx.user_id,
        )

    assert (await env.daily(ctx, between=edit_observation)).status == "stale"
    assert await env.days(ctx) == []


async def test_the_write_holds_the_revision_row_so_a_purge_waits_for_it(env: Env) -> None:
    ctx, _ = await seeded(env)
    await env.normalise(ctx)
    state = {"writing": False}
    blocked: list[bool] = []
    purge_task: list[asyncio.Task[None]] = []

    async def mark() -> None:
        state["writing"] = True

    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with user_session(env.factory, ctx) as db:
            yield db
            if state["writing"]:
                purge_task.append(asyncio.ensure_future(purge_imported(env.factory, ctx)))
                await asyncio.sleep(0.5)
                blocked.append(not purge_task[0].done())

    outcome = await daily_owner(open_tx, ctx.user_id, between=mark)
    await purge_task[0]
    assert outcome.status == "written"
    assert blocked == [True]  # the purge could not pass the row lock before the commit


async def test_two_concurrent_passes_for_one_owner_do_not_collide(env: Env) -> None:
    ctx, _ = await seeded(env)
    await env.normalise(ctx)
    arrived = 0
    both_read = asyncio.Event()

    async def rendezvous() -> None:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_read.set()
        await asyncio.wait_for(both_read.wait(), 10)

    # Both passes read an empty table, then both write the same four days.
    outcomes = await asyncio.gather(
        env.daily(ctx, between=rendezvous), env.daily(ctx, between=rendezvous)
    )
    assert sorted(o.status for o in outcomes) == ["written", "written"]
    assert sorted(o.inserted for o in outcomes) == [0, 4]  # the second found them in place
    assert len(await env.days(ctx)) == 4


async def test_only_rows_of_the_metrics_this_pass_owns_are_touched(env: Env) -> None:
    ctx, _ = await seeded(env)
    env.jenv.sql(  # a daily row of a key the pass does not own, with foreign provenance
        "INSERT INTO metric_daily (owner_id, metric_key, day, method, value, mean, min, max, "
        "mad, n, source_groups, algo_version, input_revision, input_fingerprint) VALUES "
        "(:o, 'meastype_4242', '2026-10-20', 'median', 1, 1, 1, 1, 0, 1, '{9}', 'other/7', 99, "
        "decode(repeat('ab', 32), 'hex'))",
        o=ctx.user_id,
    )
    env.jenv.sql(  # a row of an owned key for a day with no reading: removed
        "INSERT INTO metric_daily (owner_id, metric_key, day, method, value, mean, min, max, "
        "mad, n, source_groups, algo_version, input_revision, input_fingerprint) VALUES "
        "(:o, 'weight', '2026-01-01', 'median', 1, 1, 1, 1, 0, 1, '{9}', 'daily/1', 1, "
        "decode(repeat('cd', 32), 'hex'))",
        o=ctx.user_id,
    )
    outcome = await env.both(ctx)
    assert (outcome.inserted, outcome.deleted) == (4, 1)
    foreign = [r for r in await env.days(ctx) if r.metric_key == "meastype_4242"]
    assert [(r.algo_version, r.input_revision, r.source_groups) for r in foreign] == [
        ("other/7", 99, [9])
    ]
    assert bytes(foreign[0].input_fingerprint) == bytes.fromhex("ab" * 32)


async def test_only_the_owners_rows_are_read_and_written(env: Env) -> None:
    mine, my_connection = env.owner()
    other, other_connection = env.owner()
    await env.ingest(mine, my_connection, weight(1, OCT_20, 80_050))
    await env.ingest(other, other_connection, weight(1, OCT_20, 60_000))
    await env.both(mine)
    assert [str(r.value) for r in await env.days(mine)] == ["80.050"]
    assert await env.days(other) == []
    await env.both(other)
    assert [str(r.value) for r in await env.days(other)] == ["60.000"]
    assert [str(r.value) for r in await env.days(mine)] == ["80.050"]


async def test_the_job_runs_in_the_workers_context_and_sees_only_its_owner(env: Env) -> None:
    jenv = env.jenv
    mine, my_connection = env.owner()
    other, other_connection = env.owner()
    await env.ingest(mine, my_connection, weight(1, OCT_20, 80_050))
    await env.ingest(other, other_connection, weight(1, OCT_20, 60_000))
    await env.normalise(mine)
    await env.normalise(other)
    registry = KindRegistry()
    register_daily_kind(registry)
    register_daily_kind(registry)  # twice is one registration
    assert registry.names() == [KIND]
    payload = json.dumps({"connection_id": str(my_connection), "origin": "live"})
    job_id = jenv.job(mine.user_id, kind=KIND, input=payload)[1]
    claimed = await queue.claim(env.factory, "w", 60, [KIND])
    assert claimed is not None
    assert claimed.owner_id == mine.user_id
    execution = Execution(claimed)
    config = RunConfig(lease_seconds=60, accounts=jenv.accounts)
    await execute(execution, env.factory, registry, config, asyncio.Event())
    assert (execution.result, jenv.row(job_id).state) == ("completed", "completed")
    assert [str(r.value) for r in await env.days(mine)] == ["80.050"]
    assert await env.days(other) == []


async def test_enqueue_keeps_live_and_backfill_apart_and_collapses_repeats(env: Env) -> None:
    ctx, connection = env.owner()
    for origin in ("live", "backfill", "live", "backfill", "live"):
        await enqueue_daily(env.factory, connection, origin)  # pyright: ignore[reportArgumentType]
    rows = env.jenv.sql(
        "SELECT input, singleton_key, state FROM jobs WHERE owner_id = :o AND kind = :k "
        "ORDER BY singleton_key",
        o=ctx.user_id,
        k=KIND,
    ).all()
    assert [(r.singleton_key, r.state) for r in rows] == [
        ("daily_backfill", "queued"),
        ("daily_live", "queued"),
    ]
    assert [DailyInput.model_validate(r.input).origin for r in rows] == ["backfill", "live"]


def test_the_kind_is_registered_in_the_worker_only() -> None:
    assert KIND in worker_registry().names()
    assert KINDS.get(KIND) is None


async def run_normalize_job(env: Env, ctx: UserContext, connection: uuid.UUID, origin: str) -> str:
    registry = KindRegistry()
    register_normalize_kind(registry)
    payload = json.dumps({"connection_id": str(connection), "origin": origin})
    env.jenv.job(ctx.user_id, kind=NORMALIZE_KIND, input=payload)
    claimed = await queue.claim(env.factory, "w", 60, [NORMALIZE_KIND])
    assert claimed is not None
    execution = Execution(claimed)
    config = RunConfig(lease_seconds=60, accounts=env.jenv.accounts)
    await execute(execution, env.factory, registry, config, asyncio.Event())
    return str(execution.result)


def queued_daily(env: Env, ctx: UserContext) -> list[tuple[str, str]]:
    rows = env.jenv.sql(
        "SELECT singleton_key, input->>'origin' AS origin FROM jobs WHERE owner_id = :o "
        "AND kind = :k ORDER BY singleton_key",
        o=ctx.user_id,
        k=KIND,
    ).all()
    return [(r.singleton_key, r.origin) for r in rows]


async def test_the_normaliser_queues_the_daily_pass_with_its_own_origin(env: Env) -> None:
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, weight(1, OCT_20, 80_050))
    assert await run_normalize_job(env, ctx, connection, "backfill") == "completed"
    assert queued_daily(env, ctx) == [("daily_backfill", "backfill")]
    assert await run_normalize_job(env, ctx, connection, "live") == "completed"  # `current`
    assert queued_daily(env, ctx) == [("daily_backfill", "backfill"), ("daily_live", "live")]


async def test_nothing_imported_queues_no_daily_pass(env: Env) -> None:
    ctx, connection = env.owner()
    assert await run_normalize_job(env, ctx, connection, "live") == "completed"
    assert queued_daily(env, ctx) == []


def test_the_provenance_names_both_algorithms() -> None:
    assert (ALGO_VERSION, NORMALIZE_VERSION) == ("daily/1", "normalize/1")
