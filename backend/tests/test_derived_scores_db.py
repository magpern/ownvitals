"""The scores pass against the database (#188, ADR 0012): rows through ingest, the normaliser, the
daily pass; idempotence; one row per (owner, kind, day); the experimental switch for HIS (always
``not_set`` here: no band table is registered); the purge race; the worker context.

**Synthetic** users and measures only (made-up values); no real account or measurement.
"""

import uuid
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
from ownvitals.jobs import KindRegistry
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


async def test_another_owner_sees_none_of_the_rows(env: Env) -> None:
    ctx, _ = await ready(env)
    await env.score(ctx)
    other, _ = env.owner()
    assert await env.stored(other) == []


def test_the_kind_registers_once() -> None:
    registry = KindRegistry()
    register_scores_kind(registry)
    register_scores_kind(registry)
    assert registry.get(KIND) is not None
    assert ScoresInput(connection_id=uuid.uuid4())
