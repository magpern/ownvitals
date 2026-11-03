"""Normalisation against the database (#185): the rows and their provenance, idempotence,
deletion and backdating, the purge race, month partitions, the worker context and the enqueue.

Setup rows are written through the ingest code as the owner; the pass runs as ``ownvitals_app``.
Synthetic users, groups and measures only; no real account or measurement.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ctx import make_context
from jobcore import JobsEnv, jenv_fixture  # noqa: F401 - fixtures
from jobtools import admin_fixture, ensure_user, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.connections.purge import purge_imported
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.derived.normalize import ALGO_VERSION
from ownvitals.derived.normalize_db import Outcome, normalise_owner
from ownvitals.derived.normalize_job import (
    KIND,
    NormalizeInput,
    enqueue_normalize,
    register_normalize_kind,
)
from ownvitals.ingest.revisions import current
from ownvitals.ingest.rows import soft_delete_measure, upsert_measure_group
from ownvitals.jobs import KINDS, KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.jobs.withings import StreamInput
from ownvitals.withings import sync
from ownvitals.withings.models import MeasureGroup
from ownvitals.worker.kinds import worker_registry

pytestmark = pytest.mark.usefixtures("jenv")

NOW = datetime(2026, 11, 1, tzinfo=UTC)
OCT_20 = 1_792_477_800  # 2026-10-20 06:30 UTC
OCT_21 = OCT_20 + 86_400
MAR_2020 = 1_583_051_400  # 2020-03-01 08:30 UTC
SNAPSHOT = (
    "SELECT id, metric_key, ts, source_tz, value, source_value_raw, source_unit, conversion, "
    "quality_flags, source_grpid, source_type, source_position, algo_version, input_revision, "
    "input_fingerprint FROM observations WHERE owner_id = :o ORDER BY metric_key, ts, source_grpid"
)


def group(grpid: int, when: int, *measures: tuple[int, int, int, int | None], **extra: Any) -> Any:
    """A synthetic measure group; each measure is ``(type, value, unit, position)``."""
    return MeasureGroup.model_validate(
        {
            "grpid": grpid,
            "date": when,
            "category": extra.pop("category", 1),
            "attrib": extra.pop("attrib", 0),
            "timezone": "Europe/Stockholm",
            "measures": [
                {"type": t, "value": v, "unit": u, **({} if p is None else {"position": p})}
                for t, v, u, p in measures
            ],
            **extra,
        }
    )


class Env:
    def __init__(self, jenv: JobsEnv) -> None:
        self.jenv = jenv
        self.factory = jenv.factory

    def owner(self) -> tuple[UserContext, uuid.UUID]:
        owner, connection = uuid.uuid4(), uuid.uuid4()
        ensure_user(self.jenv.admin, owner)
        self.jenv.sql(
            "INSERT INTO connections (id, owner_id, provider, status, public) "
            "VALUES (:id, :owner, 'withings', 'needs_reconnect', '{}')",
            id=connection,
            owner=owner,
        )
        return make_context(owner), connection

    async def ingest(self, ctx: UserContext, connection: uuid.UUID, *groups: Any) -> None:
        async with user_session(self.factory, ctx) as db:
            for g in groups:
                await upsert_measure_group(db, ctx.user_id, connection, g, "Europe/Stockholm", NOW)

    def opener(self, ctx: UserContext) -> Any:
        @asynccontextmanager
        async def open_tx() -> AsyncIterator[AsyncSession]:
            async with user_session(self.factory, ctx) as db:
                yield db

        return open_tx

    async def run(self, ctx: UserContext, **kw: Any) -> Outcome:
        return await normalise_owner(self.opener(ctx), ctx.user_id, now=NOW, **kw)

    async def rows(self, ctx: UserContext) -> list[Any]:
        async with user_session(self.factory, ctx) as db:
            return list((await db.execute(text(SNAPSHOT), {"o": ctx.user_id})).all())


@pytest.fixture
def env(jenv: JobsEnv) -> Env:
    return Env(jenv)


WEIGHT = group(1, OCT_20, (1, 80_050, -3, None), (11, 61, 0, None), (4242, 17, 0, None))
SEGMENTS = group(2, OCT_21, (175, 3_100, -3, 3), (175, 3_200, -3, 2), (173, 9_000, -3, 3))


async def test_a_pass_writes_the_observations_with_their_provenance(env: Env) -> None:
    ctx, connection = env.owner()
    await env.ingest(
        ctx, connection, WEIGHT, SEGMENTS, group(3, OCT_20, (1, 70_000, -3, None), attrib=1)
    )
    outcome = await env.run(ctx)
    assert (outcome.status, outcome.inserted, outcome.updated, outcome.deleted) == (
        "written",
        7,
        0,
        0,
    )
    rows = await env.rows(ctx)
    by = {(r.metric_key, r.source_grpid): r for r in rows}
    weight = by["weight", 1]
    assert str(weight.value) == "80.050"
    assert weight.conversion == "e-3:kg"
    assert (weight.source_value_raw, weight.source_unit, weight.source_tz) == (
        80_050, -3, "Europe/Stockholm",
    )  # fmt: skip
    assert (weight.source_type, weight.source_position) == (1, None)
    assert by["meastype_4242", 1].conversion == "e0:unknown"
    assert by["segmental_muscle_mass:left_arm", 2].source_position == 3
    assert by["segmental_muscle_mass:right_arm", 2].source_position == 2
    assert list(by["weight", 3].quality_flags) == ["unconfirmed"]
    async with user_session(env.factory, ctx) as db:
        revision = await current(db, ctx.user_id, "measures")
    assert {(r.algo_version, r.input_revision, bytes(r.input_fingerprint)) for r in rows} == {
        (ALGO_VERSION, revision, bytes(rows[0].input_fingerprint))
    }
    assert len(bytes(rows[0].input_fingerprint)) == 32


async def test_running_twice_gives_identical_rows(env: Env) -> None:
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, WEIGHT, SEGMENTS)
    assert (await env.run(ctx)).status == "written"
    first = await env.rows(ctx)
    again = await env.run(ctx)
    assert again.status == "current"
    assert await env.rows(ctx) == first  # same ids, values and provenance
    # Forget the provenance: the pass recomputes, finds the same rows and changes nothing else.
    env.jenv.sql("UPDATE observations SET input_revision = 0 WHERE owner_id = :o", o=ctx.user_id)
    redone = await env.run(ctx)
    assert (redone.status, redone.inserted, redone.updated, redone.deleted) == ("written", 0, 0, 0)
    assert await env.rows(ctx) == first
    # Unchanged source rows are not a change: the revision stays and the pass stays current.
    await env.ingest(ctx, connection, WEIGHT)
    assert (await env.run(ctx)).status == "current"


async def test_a_deleted_group_and_a_backdated_group_change_the_observations(env: Env) -> None:
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, WEIGHT, SEGMENTS)
    await env.run(ctx)
    before = await env.rows(ctx)
    async with user_session(env.factory, ctx) as db:
        assert await soft_delete_measure(db, ctx.user_id, connection, 2)
        revision = await current(db, ctx.user_id, "measures")
    outcome = await env.run(ctx)
    assert (outcome.status, outcome.deleted, outcome.inserted) == ("written", 3, 0)
    rows = await env.rows(ctx)
    assert {r.source_grpid for r in rows} == {1}
    assert {r.input_revision for r in rows} == {revision}
    assert {r.input_revision for r in before} != {revision}
    # Backdate group 1 by a day: the old instant goes, the new one arrives.
    await env.ingest(ctx, connection, group(1, OCT_20 - 86_400, (1, 80_050, -3, None)))
    outcome = await env.run(ctx)
    assert (outcome.deleted, outcome.inserted) == (3, 1)
    rows = await env.rows(ctx)
    assert [(r.metric_key, r.ts.isoformat()) for r in rows] == [
        ("weight", "2026-10-19T06:30:00+00:00")
    ]
    # Undelete group 2 (a changed payload clears the mark): its rows come back.
    await env.ingest(ctx, connection, group(2, OCT_21, (175, 3_100, -3, 3)))
    assert (await env.run(ctx)).inserted == 1


async def test_objectives_are_not_readings(env: Env) -> None:
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, WEIGHT, group(9, OCT_20, (1, 65_000, -3, None), category=2))
    await env.run(ctx)
    assert {r.source_grpid for r in await env.rows(ctx)} == {1}


async def test_a_purge_between_the_read_and_the_write_writes_nothing(env: Env) -> None:
    """The review of #225 (issue #187's comment): a pass that read before "delete imported data"
    must not write after it."""
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, WEIGHT, SEGMENTS)

    async def purge() -> None:
        await purge_imported(env.factory, ctx)

    outcome = await env.run(ctx, between=purge)
    assert outcome.status == "stale"
    assert await env.rows(ctx) == []
    assert (await env.run(ctx)).status == "nothing"  # no revision row, nothing to read
    assert await env.rows(ctx) == []


async def test_a_purge_and_the_same_data_again_is_still_stale(env: Env) -> None:
    """The revision counter restarts at the old value; the fingerprint (new group ids) differs."""
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, WEIGHT)
    async with user_session(env.factory, ctx) as db:
        assert await current(db, ctx.user_id, "measures") == 1

    async def purge_and_ingest_again() -> None:
        await purge_imported(env.factory, ctx)
        await env.ingest(ctx, connection, WEIGHT)

    outcome = await env.run(ctx, between=purge_and_ingest_again)
    assert outcome.status == "stale"
    assert await env.rows(ctx) == []
    assert (await env.run(ctx)).status == "written"  # the next pass sees the new inputs


async def test_a_new_ingest_between_the_read_and_the_write_writes_nothing(env: Env) -> None:
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, WEIGHT)

    async def ingest_more() -> None:
        await env.ingest(ctx, connection, SEGMENTS)

    assert (await env.run(ctx, between=ingest_more)).status == "stale"
    assert await env.rows(ctx) == []
    assert (await env.run(ctx)).status == "written"


async def test_the_write_holds_the_revision_row_so_a_purge_waits_for_it(env: Env) -> None:
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, WEIGHT)
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

    outcome = await normalise_owner(open_tx, ctx.user_id, between=mark, now=NOW)
    await purge_task[0]
    assert outcome.status == "written"
    assert blocked == [True]  # the purge could not pass the row lock before the commit


async def test_old_and_new_months_get_their_partitions(env: Env) -> None:
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, group(5, MAR_2020, (1, 80_000, -3, None)), WEIGHT)
    assert (await env.run(ctx)).inserted == 4
    months = {r.ts.strftime("%Y-%m") for r in await env.rows(ctx)}
    assert months == {"2020-03", "2026-10"}


async def test_only_the_owners_rows_are_read_and_written(env: Env) -> None:
    mine, my_connection = env.owner()
    other, other_connection = env.owner()
    await env.ingest(mine, my_connection, WEIGHT)
    await env.ingest(other, other_connection, SEGMENTS)
    await env.run(mine)
    assert {r.source_grpid for r in await env.rows(mine)} == {1}
    assert await env.rows(other) == []
    await env.run(other)
    assert {r.metric_key.split(":")[0] for r in await env.rows(other)} == {
        "segmental_muscle_mass", "segmental_fat_free_mass",
    }  # fmt: skip
    assert {r.source_grpid for r in await env.rows(mine)} == {1}


async def test_the_job_runs_in_the_workers_context_and_sees_only_its_owner(env: Env) -> None:
    jenv = env.jenv
    mine, my_connection = env.owner()
    other, other_connection = env.owner()
    await env.ingest(mine, my_connection, WEIGHT)
    await env.ingest(other, other_connection, SEGMENTS)
    registry = KindRegistry()
    register_normalize_kind(registry)
    register_normalize_kind(registry)  # twice is one registration
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
    assert {r.source_grpid for r in await env.rows(mine)} == {1}
    assert await env.rows(other) == []


async def test_enqueue_keeps_live_and_backfill_apart_and_collapses_repeats(env: Env) -> None:
    jenv = env.jenv
    ctx, connection = env.owner()
    for origin in ("live", "backfill", "live", "backfill", "live"):
        await enqueue_normalize(env.factory, connection, origin)  # pyright: ignore[reportArgumentType]
    rows = jenv.sql(
        "SELECT input, singleton_key, state FROM jobs WHERE owner_id = :o AND kind = :k "
        "ORDER BY singleton_key",
        o=ctx.user_id,
        k=KIND,
    ).all()
    assert [(r.singleton_key, r.state) for r in rows] == [
        ("normalize_backfill", "queued"),
        ("normalize_live", "queued"),
    ]
    assert [NormalizeInput.model_validate(r.input).origin for r in rows] == ["backfill", "live"]


def test_the_worker_registers_the_kind_and_the_api_does_not() -> None:
    assert KIND in worker_registry().names()
    assert KINDS.get(KIND) is None


class _Client:
    @classmethod
    @asynccontextmanager
    async def open(cls, app: object, **_options: object) -> AsyncIterator[str]:
        yield "client"


class _Job:
    def __init__(self, parsed: object) -> None:
        self.input = parsed
        self.factory = "factory"


async def test_the_normalised_streams_queue_a_pass_and_the_others_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued: list[tuple[object, uuid.UUID, str]] = []

    async def remember(factory: object, connection_id: uuid.UUID, origin: str) -> None:
        queued.append((factory, connection_id, origin))

    async def materials(job: object, connection_id: uuid.UUID) -> tuple[str, str]:
        return "account", "app"

    async def done(*args: object, **kwargs: object) -> str:
        return "supported"

    monkeypatch.setattr(sync, "enqueue_normalize", remember)
    monkeypatch.setattr(sync, "open_materials", materials)
    monkeypatch.setattr(sync, "WithingsClient", _Client)
    monkeypatch.setattr(sync, "execute_backfill", done)
    monkeypatch.setattr(sync, "execute_sync", done)
    connection = uuid.uuid4()
    # #229: the sleep summaries, activity days and workouts are normalised too; the two
    # high-volume series are not.
    queuing = ("measures", "sleep_summary", "activity_daily", "workouts")
    for stream in (*queuing, "sleep_series", "intraday"):
        parsed = StreamInput(connection_id=connection, stream=stream)
        await sync.run_backfill(_Job(parsed))  # type: ignore[arg-type]
        await sync.run_sync_kind(_Job(parsed))  # type: ignore[arg-type]
    assert queued == [
        # the backfill and the incremental pass stay apart
        ("factory", connection, origin)
        for _ in queuing
        for origin in ("backfill", "live")
    ]


async def test_a_moved_counter_alone_is_stale(env: Env) -> None:
    """The revision is checked on its own, not only through the fingerprint."""
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, WEIGHT)

    async def bump_only() -> None:
        env.jenv.sql(
            "UPDATE data_revisions SET revision = revision + 1 WHERE owner_id = :o",
            o=ctx.user_id,
        )

    assert (await env.run(ctx, between=bump_only)).status == "stale"
    assert await env.rows(ctx) == []
