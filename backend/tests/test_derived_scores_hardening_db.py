"""Hardening of the scores pass (#302): concurrent writers, the fingerprint cases, the latest day
across a zone change, the staleness inputs a read covers, the unambiguous ``input_revision``.

**Synthetic** users and measures only (made-up values); no real account or measurement.
"""

import asyncio
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text

from jobcore import JobsEnv, jenv_fixture  # noqa: F401 - fixtures
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.derived.scores_revision import revision_digest
from ownvitals.derived.scores_store import score_owner
from ownvitals.ingest.rows import soft_delete_measure
from ownvitals.views.reads import reading
from test_derived_evaluate_db import YEAR, year_groups
from test_derived_scores_db import NO_TABLE, Env, ready
from test_derived_scores_db import env as env

pytestmark = pytest.mark.usefixtures("jenv")

SYNTHETIC = True
FINGERPRINTS = "SELECT DISTINCT input_fingerprint AS f FROM scores"
REVISIONS = "SELECT DISTINCT input_revision AS r FROM scores"


async def column(env: Env, ctx: UserContext, sql: str) -> set[Any]:
    async with user_session(env.factory, ctx) as db:
        return {
            bytes(r[0]) if isinstance(r[0], memoryview) else r[0]
            for r in await db.execute(text(sql))
        }


async def revisions_of(env: Env, ctx: UserContext) -> tuple[int, int, int]:
    async with user_session(env.factory, ctx) as db:
        rows = (await db.execute(text("SELECT stream, revision FROM data_revisions"))).all()
    found = {r.stream: int(r.revision) for r in rows}
    return (found.get("measures", 0), found.get("sleep_summary", 0), found.get("activity_daily", 0))


# --- concurrent writers --------------------------------------------------------------------


async def test_two_concurrent_passes_over_the_same_data_write_one_row_set(env: Env) -> None:
    ctx, _ = await ready(env)
    first, second = await asyncio.gather(env.score(ctx), env.score(ctx))
    assert sorted([first.status, second.status]) == ["current", "written"]
    assert len(await env.stored(ctx)) == len(await column(env, ctx, "SELECT kind FROM scores"))


async def test_a_writer_that_lost_the_race_to_a_newer_pass_writes_nothing(env: Env) -> None:
    """Pass A read the old data; before it writes, new data is ingested and pass B stores the
    result for it. A must not overwrite B's rows with the old inputs."""
    ctx, connection = await ready(env)

    async def newer_pass() -> None:
        await env.ingest(ctx, connection, *year_groups(range(YEAR, YEAR + 1)))
        await env.derive(ctx)
        assert (await env.score(ctx)).status == "written"

    assert (await env.score(ctx, between=newer_pass)).status == "stale"
    assert await column(env, ctx, REVISIONS) == {revision_digest(await revisions_of(env, ctx))}


async def test_a_fingerprint_change_alone_between_read_and_write_is_stale(env: Env) -> None:
    """The revisions are the ones read; only an input moved (a daily value edited in place)."""
    ctx, _ = await ready(env)
    before = await revisions_of(env, ctx)

    async def edit() -> None:
        env.jenv.sql(
            "UPDATE metric_daily SET value = value + 1, morning = morning + 1, min = min + 1, "
            "max = max + 1, mean = mean + 1 WHERE owner_id = :o "
            "AND metric_key = 'weight' AND day = (SELECT max(day) FROM metric_daily "
            "WHERE owner_id = :o AND metric_key = 'weight')",
            o=ctx.user_id,
        )

    assert (await env.score(ctx, between=edit)).status == "stale"
    assert await revisions_of(env, ctx) == before
    assert await env.stored(ctx) == []


# --- fingerprint cases -----------------------------------------------------------------------


async def test_a_changed_payload_with_the_same_values_changes_the_fingerprint(env: Env) -> None:
    ctx, connection = await ready(env)
    await env.score(ctx)
    first = await column(env, ctx, FINGERPRINTS)
    (last,) = year_groups(range(YEAR - 1, YEAR))
    # the same measures, another payload (hence hash): the stream revision moves
    await env.ingest(ctx, connection, last.model_copy(update={"model_id": 99}))
    await env.derive(ctx)
    assert (await env.score(ctx)).status == "written"
    assert await column(env, ctx, FINGERPRINTS) != first


async def test_a_soft_deleted_input_changes_the_fingerprint_and_the_row(env: Env) -> None:
    ctx, connection = await ready(env)
    await env.score(ctx)
    first, first_revision = await column(env, ctx, FINGERPRINTS), await column(env, ctx, REVISIONS)
    async with user_session(env.factory, ctx) as db:
        assert await soft_delete_measure(db, ctx.user_id, connection, 1000 + YEAR - 1)
    await env.derive(ctx)
    assert (await env.score(ctx)).status == "written"
    assert await column(env, ctx, FINGERPRINTS) != first
    assert await column(env, ctx, REVISIONS) != first_revision


# --- the latest day across a zone change ---------------------------------------------------


async def test_the_latest_day_follows_a_changed_fixed_zone(env: Env) -> None:
    """The day of ``now`` is read in the zone in force: a user whose fixed zone changes gets a
    row for the new day, and the earlier day stays as history."""
    ctx, _ = await ready(env)
    late = datetime(2026, 11, 1, 23, 30, tzinfo=UTC)  # 2 Nov in Stockholm, 1 Nov in UTC

    async def score_in(zone: str) -> Any:
        env.jenv.sql(
            "INSERT INTO user_profiles (owner_id, day_assignment, day_tz) "
            "VALUES (:o, 'fixed_tz', :z) ON CONFLICT (owner_id) DO UPDATE "
            "SET day_assignment = 'fixed_tz', day_tz = EXCLUDED.day_tz",
            o=ctx.user_id,
            z=zone,
        )
        return await score_owner(
            env.opener(ctx), ctx.user_id, settings=env.settings(), table=NO_TABLE, now=late
        )

    assert (await score_in("Europe/Stockholm")).status == "written"
    assert {r.day for r in await env.stored(ctx)} == {date(2026, 11, 2)}
    assert (await score_in("UTC")).status == "written"
    assert {r.day for r in await env.stored(ctx)} == {date(2026, 11, 1), date(2026, 11, 2)}


# --- the unambiguous input revision, and what a read treats as stale ---------------------------


def test_the_digest_tells_apart_counter_sets_with_the_same_sum() -> None:
    sets = [(2, 0, 0), (1, 1, 0), (0, 2, 0), (0, 1, 1), (0, 0, 2), (1, 0, 1)]
    digests = {revision_digest(s) for s in sets}
    assert len(digests) == len(sets)
    assert revision_digest((1, 2, 3)) == revision_digest((1, 2, 3))
    assert all(0 <= d < 2**63 for d in digests)  # fits the bigint >= 0 column


async def stale_of(env: Env, ctx: UserContext) -> list[bool]:
    async with reading(env.factory, ctx) as reader:
        return [stale for _, stale in await reader.latest_scores(settings=env.settings())]


async def test_a_read_is_stale_for_a_stream_counter_set_with_an_equal_sum(env: Env) -> None:
    ctx, _ = await ready(env)
    await env.score(ctx)
    assert set(await stale_of(env, ctx)) == {False}
    (measures,) = (await revisions_of(env, ctx))[:1]
    # the stored digest is of (n, 0, 0); the counters are now (n - 1, 1, 0): the same sum
    env.jenv.sql(
        "UPDATE data_revisions SET revision = revision - 1 WHERE owner_id = :o "
        "AND stream = 'measures'",
        o=ctx.user_id,
    )
    env.jenv.sql(
        "INSERT INTO data_revisions (owner_id, stream, revision) VALUES (:o, 'sleep_summary', 1)",
        o=ctx.user_id,
    )
    assert measures >= 1
    assert set(await stale_of(env, ctx)) == {True}


async def test_a_read_covers_the_stream_revisions_only(env: Env) -> None:
    """Decision (#302): staleness on read is the stream revisions. A profile, zone, switch or
    band-table change is not seen until the next pass (the fingerprint is not recomputed on a
    read); the pass that follows rewrites the row. Documented in docs/dev/derived.md."""
    ctx, connection = await ready(env)
    await env.score(ctx)
    env.jenv.sql(
        "INSERT INTO user_profiles (owner_id, birth_year, sex) VALUES (:o, 1980, 'male') "
        "ON CONFLICT (owner_id) DO UPDATE SET birth_year = 1980, sex = 'male'",
        o=ctx.user_id,
    )
    assert set(await stale_of(env, ctx)) == {False}  # out of scope on a read ...
    assert (await env.score(ctx)).status == "written"  # ... and caught by the next pass
    await env.ingest(ctx, connection, *year_groups(range(YEAR, YEAR + 1)))
    assert set(await stale_of(env, ctx)) == {True}  # a stream revision is covered
