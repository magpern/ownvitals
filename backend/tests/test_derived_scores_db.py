"""The scores pass against the database (#188, ADR 0012): rows through ingest, the normaliser, the
daily pass; idempotence; one row per (owner, kind, day); the experimental switch for HIS (always
``not_set`` here: no band table is registered); the purge race; the worker context.

**Synthetic** users and measures only (made-up values); no real account or measurement.
"""

import asyncio
import json
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text

from jobcore import JobsEnv, jenv_fixture  # noqa: F401 - fixtures
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.connections.purge import purge_imported
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.derived.scores_job import KIND, ScoresInput, register_scores_kind
from ownvitals.derived.scores_store import Outcome, score_owner
from ownvitals.derived.scores_tables import TableState
from ownvitals.ingest.rows import upsert_sleep_summary
from ownvitals.jobs import KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.withings.models import SleepSummary
from test_derived_evaluate_db import NOW, TODAY, YEAR, a_year, year_groups
from test_derived_evaluate_db import Env as EvalEnv

pytestmark = pytest.mark.usefixtures("jenv")

NO_TABLE = TableState(None, None, "")
ROWS = (
    "SELECT kind, day, value, components, input_revision FROM scores "
    "WHERE owner_id = :o ORDER BY kind, day"
)


class Env(EvalEnv):
    async def score(self, ctx: UserContext, *, experimental: bool = False, **kw: Any) -> Outcome:
        return await score_owner(
            self.opener(ctx),
            ctx.user_id,
            settings=self.settings(experimental=experimental),
            table=NO_TABLE,
            now=NOW,
            **kw,
        )

    async def stored(self, ctx: UserContext) -> list[Any]:
        async with user_session(self.factory, ctx) as db:
            return list((await db.execute(text(ROWS), {"o": ctx.user_id})).all())


@pytest.fixture
def env(jenv: JobsEnv) -> Env:
    return Env(jenv)


async def ready(env: Env) -> tuple[UserContext, uuid.UUID]:
    ctx, connection = await a_year(env)
    await env.derive(ctx)
    return ctx, connection


async def test_a_pass_writes_one_row_per_kind_for_the_day(env: Env) -> None:
    ctx, _ = await ready(env)
    outcome = await env.score(ctx)
    assert outcome.status == "written"
    rows = await env.stored(ctx)
    assert {r.kind.split(":")[0] for r in rows} == {"body_path", "readiness"}
    assert all(r.day == TODAY for r in rows)
    assert all("status" in r.components for r in rows)  # a value is set only when computed
    assert all(r.value is None or r.components["status"] == "computed" for r in rows)


async def test_a_second_pass_changes_nothing(env: Env) -> None:
    ctx, _ = await ready(env)
    await env.score(ctx)
    first = await env.stored(ctx)
    again = await env.score(ctx)
    assert (again.status, again.changed) == ("current", 0)
    assert await env.stored(ctx) == first
    assert len({(r.kind, r.day) for r in first}) == len(first)  # one row per (kind, day)


async def test_his_is_not_written_without_the_switch_and_is_not_set_with_it(env: Env) -> None:
    ctx, _ = await ready(env)
    await env.score(ctx)
    assert "his" not in [r.kind for r in await env.stored(ctx)]
    await env.score(ctx, experimental=True)
    his = [r for r in await env.stored(ctx) if r.kind == "his"]
    assert len(his) == 1
    assert his[0].value is None  # no band table registered (ADR 0012): never a number
    assert his[0].components["status"] == "not_set"
    await env.score(ctx)  # switch off again: the stored experimental row is deleted
    assert "his" not in [r.kind for r in await env.stored(ctx)]


async def test_a_purge_between_the_read_and_the_write_writes_nothing(env: Env) -> None:
    ctx, _ = await ready(env)

    async def purge() -> None:
        await purge_imported(env.factory, ctx)

    assert (await env.score(ctx, between=purge)).status == "stale"
    assert await env.stored(ctx) == []


async def test_a_new_ingest_between_the_read_and_the_write_writes_nothing(env: Env) -> None:
    ctx, connection = await ready(env)

    async def ingest_more() -> None:
        await env.ingest(ctx, connection, *year_groups(range(YEAR, YEAR + 1)))

    assert (await env.score(ctx, between=ingest_more)).status == "stale"
    assert await env.stored(ctx) == []


async def test_rls_hides_another_owners_rows_without_any_owner_filter(env: Env) -> None:
    """No ``owner_id`` in the SQL: only forced RLS can hide the rows."""
    ctx, _ = await ready(env)
    await env.score(ctx)
    other, _ = env.owner()
    async with user_session(env.factory, other) as db:
        assert (await db.execute(text("SELECT count(*) FROM scores"))).scalar_one() == 0
    async with user_session(env.factory, ctx) as db:
        assert (await db.execute(text("SELECT count(*) FROM scores"))).scalar_one() > 0


async def run_scores_job(env: Env, ctx: UserContext, connection: uuid.UUID) -> str:
    registry = KindRegistry()
    register_scores_kind(registry)
    payload = json.dumps({"connection_id": str(connection)})
    env.jenv.job(ctx.user_id, kind=KIND, input=payload)
    claimed = await queue.claim(env.factory, "w", 60, [KIND])
    assert claimed is not None
    execution = Execution(claimed)
    config = RunConfig(lease_seconds=60, accounts=env.jenv.accounts)
    await execute(execution, env.factory, registry, config, asyncio.Event())
    return str(execution.result)


def admin_rows(env: Env, owner: uuid.UUID) -> list[Any]:
    return list(env.jenv.sql("SELECT kind, day FROM scores WHERE owner_id = :o", o=owner).all())


async def test_the_job_writes_for_its_owner_only_in_the_workers_context(env: Env) -> None:
    mine, my_connection = await ready(env)
    other, _ = await a_year(env)
    await env.derive(other)
    assert await run_scores_job(env, mine, my_connection) == "completed"
    assert admin_rows(env, mine.user_id) != []
    assert admin_rows(env, other.user_id) == []


def sleep_summary() -> SleepSummary:
    """A synthetic sleep summary (``synthetic: true``): 7 h 10 min, Europe/Stockholm."""
    return SleepSummary.model_validate(
        {
            "id": 9001,
            "startdate": 1_792_446_900,
            "enddate": 1_792_474_800,
            "timezone": "Europe/Stockholm",
            "data": {"total_sleep_time": 25_800, "hr_average": 54},
        }
    )


async def test_a_sleep_only_owner_gets_a_scores_job_and_a_readiness_row(env: Env) -> None:
    from ownvitals.derived.normalize_job import KIND as NORMALIZE_KIND
    from ownvitals.derived.normalize_job import register_normalize_kind

    ctx, connection = env.owner()
    async with user_session(env.factory, ctx) as db:
        await upsert_sleep_summary(db, ctx.user_id, connection, sleep_summary(), "UTC", NOW)
    registry = KindRegistry()
    register_normalize_kind(registry)
    payload = json.dumps({"connection_id": str(connection), "origin": "live"})
    env.jenv.job(ctx.user_id, kind=NORMALIZE_KIND, input=payload)
    claimed = await queue.claim(env.factory, "w", 60, [NORMALIZE_KIND])
    assert claimed is not None
    config = RunConfig(lease_seconds=60, accounts=env.jenv.accounts)
    await execute(Execution(claimed), env.factory, registry, config, asyncio.Event())
    queued = env.jenv.sql(
        "SELECT count(*) FROM jobs WHERE owner_id = :o AND kind = :k", o=ctx.user_id, k=KIND
    ).scalar_one()
    assert queued == 1  # no measures, so no daily pass: the scores are queued from here
    assert (await env.score(ctx)).status == "written"
    assert "readiness" in [r.kind for r in await env.stored(ctx)]


async def test_the_day_follows_the_zone_of_the_latest_reading(env: Env) -> None:
    ctx, _ = await ready(env)  # readings carry Europe/Stockholm
    late = datetime(2026, 11, 1, 23, 30, tzinfo=UTC)  # still 1 Nov in UTC, already 2 Nov there
    outcome = await score_owner(
        env.opener(ctx), ctx.user_id, settings=env.settings(), table=NO_TABLE, now=late
    )
    assert outcome.status == "written"
    assert {r.day for r in await env.stored(ctx)} == {date(2026, 11, 2)}


async def test_the_fingerprint_follows_the_data_and_a_recompute_keeps_it(env: Env) -> None:
    ctx, connection = await ready(env)
    await env.score(ctx)

    async def fingerprints() -> set[bytes]:
        async with user_session(env.factory, ctx) as db:
            rows = (await db.execute(text("SELECT input_fingerprint FROM scores"))).all()
        return {bytes(r.input_fingerprint) for r in rows}

    first = await fingerprints()
    assert len(first) == 1  # one pass, one fingerprint
    assert (await env.score(ctx)).status == "current"
    assert await fingerprints() == first
    await env.ingest(ctx, connection, *year_groups(range(YEAR, YEAR + 1)))
    await env.derive(ctx)
    assert (await env.score(ctx)).status == "written"
    assert await fingerprints() != first


async def test_a_score_pass_leaves_findings_and_the_outbox_unchanged(env: Env) -> None:
    ctx, _ = await ready(env)
    await env.evaluate(ctx)
    findings = "SELECT * FROM findings WHERE owner_id = :o ORDER BY id"
    outbox = "SELECT count(*) FROM mail_outbox"
    before = env.jenv.sql(findings, o=ctx.user_id).all()
    mail = env.jenv.sql(outbox).scalar_one()
    assert (await env.score(ctx)).status == "written"
    assert env.jenv.sql(findings, o=ctx.user_id).all() == before
    assert env.jenv.sql(outbox).scalar_one() == mail


def test_the_kind_registers_once() -> None:
    registry = KindRegistry()
    register_scores_kind(registry)
    register_scores_kind(registry)
    assert registry.get(KIND) is not None
    assert ScoresInput(connection_id=uuid.uuid4())
