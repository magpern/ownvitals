"""The evaluation's database checks and prepared runs (``ownvitals.db.synthetic_jobs``; ADR 0014
section 9; #527), on the ``_ai_eval`` test database (``evaldb``).

Each refusal is fail-closed and has a test that fails if its check is removed: the database's
name, another database in its cluster, ``alembic_version`` against ``SCHEMA_HEAD``, and an
account not marked ``users.synthetic``. The test cluster also holds the test database, so the
cluster check refuses there; a test that needs it to pass names the cluster's other databases
as system ones (``alone_in_cluster``), and only that test.

Synthetic accounts only (``ai-evaluation.invalid`` addresses).
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from conftest import RoleUrls
from dbtools import alembic_config
from evaldb import (  # noqa: F401 - fixtures
    EvalDb,
    eval_clean_fixture,
    eval_db_fixture,
    factory_fixture,
    owner_engine_fixture,
)
from ownvitals.ai.eval_seed import seed_accounts
from ownvitals.ai.eval_seed_model import PLANS
from ownvitals.db import synthetic_jobs
from ownvitals.db.synthetic_jobs import (
    EVAL_RUN_KIND,
    SCHEMA_HEAD,
    SyntheticJobRefused,
    cluster_refusal,
    database_refusal,
    queue_run,
    schema_refusal,
)

SYNTHETIC = True
STEADY = PLANS[0]


def test_schema_head_is_alembics_head() -> None:
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    assert heads == [SCHEMA_HEAD], (
        "a new migration was added: set SCHEMA_HEAD in ownvitals/db/synthetic_jobs.py to "
        f"{heads} so the evaluation accepts the new schema"
    )


@pytest.fixture(name="alone_in_cluster")
async def alone_in_cluster_fixture(
    owner_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cluster check passes: its other databases (the test database) count as system ones."""
    async with owner_engine.connect() as conn:
        names = (await conn.execute(text("SELECT datname FROM pg_database"))).scalars().all()
    others = {str(n) for n in names if not str(n).endswith("_ai_eval")}
    monkeypatch.setattr(synthetic_jobs, "SYSTEM_DATABASES", frozenset(others))


async def _person(eval_clean: EvalDb) -> UUID:
    """An unmarked account, as a sign-up would make it."""
    engine = create_async_engine(eval_clean.app)
    try:
        async with engine.begin() as conn:
            row = await conn.execute(
                text(
                    "INSERT INTO users (email, email_normalized, email_verified_at) "
                    "VALUES (:e, :e, now()) RETURNING id"
                ),
                {"e": "person@example.test"},
            )
            return row.scalar_one()
    finally:
        await engine.dispose()


def _jobs(eval_db: EvalDb) -> list[Any]:
    """Every job, read as the superuser (past RLS)."""
    engine = create_engine(eval_db.superuser)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT j.owner_id, j.kind, j.origin, j.state, j.input, j.max_attempts, "
                    "(SELECT count(*) FROM job_events e WHERE e.job_id = j.id) FROM jobs j"
                )
            )
            return list(rows.all())
    finally:
        engine.dispose()


async def _queue(owner_engine: AsyncEngine, user_id: UUID, plan: str = STEADY.name) -> UUID:
    async with owner_engine.begin() as conn:
        return await queue_run(conn, user_id, plan)


async def test_another_database_in_the_cluster_is_refused(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as conn:
        assert await cluster_refusal(conn) == "shared_cluster"


async def test_a_database_not_named_for_the_evaluation_is_refused(
    role_urls: RoleUrls, alone_in_cluster: None
) -> None:
    engine = create_async_engine(role_urls.owner)  # the test database itself
    try:
        async with engine.connect() as conn:
            assert await cluster_refusal(conn) == "not_an_evaluation_database"
    finally:
        await engine.dispose()


async def test_the_evaluation_database_alone_passes(
    owner_engine: AsyncEngine, alone_in_cluster: None
) -> None:
    async with owner_engine.connect() as conn:
        assert await cluster_refusal(conn) is None
        assert await schema_refusal(conn) is None


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE alembic_version SET version_num = '0030_older'",
        "INSERT INTO alembic_version VALUES ('0099_branch')",
        "DELETE FROM alembic_version",
        "ALTER TABLE alembic_version RENAME TO alembic_version_gone",
    ],
)
async def test_a_schema_not_at_head_is_refused(owner_engine: AsyncEngine, change: str) -> None:
    async with owner_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text(change))
            assert await schema_refusal(conn) == "schema_not_at_head"
        finally:
            await trans.rollback()


async def test_a_run_is_queued_once_for_a_marked_account(
    owner_engine: AsyncEngine, eval_db: EvalDb, alone_in_cluster: None
) -> None:
    user_id = (await seed_accounts(owner_engine, (STEADY,)))[STEADY.name]
    first = await _queue(owner_engine, user_id)
    assert await _queue(owner_engine, user_id) == first
    rows = _jobs(eval_db)
    assert len(rows) == 1
    owner, kind, origin, state, payload, attempts, events = rows[0]
    assert (owner, kind, origin, state, attempts, events) == (
        user_id, EVAL_RUN_KIND, "operator", "queued", 1, 1
    )  # fmt: skip
    assert payload == {"plan": STEADY.name, "revision": SCHEMA_HEAD}


async def test_the_account_setting_does_not_outlive_the_queueing(
    owner_engine: AsyncEngine, alone_in_cluster: None
) -> None:
    user_id = (await seed_accounts(owner_engine, (STEADY,)))[STEADY.name]
    async with owner_engine.begin() as conn:
        await queue_run(conn, user_id, STEADY.name)
        inside = (await conn.execute(text("SELECT current_setting('app.user_id', true)"))).scalar()
    async with owner_engine.connect() as conn:
        after = (await conn.execute(text("SELECT current_setting('app.user_id', true)"))).scalar()
    assert inside == ""
    assert after in (None, "")


async def test_an_unmarked_account_is_refused(
    owner_engine: AsyncEngine, eval_clean: EvalDb, alone_in_cluster: None
) -> None:
    person = await _person(eval_clean)
    with pytest.raises(SyntheticJobRefused) as refused:
        await _queue(owner_engine, person)
    assert refused.value.code == "unmarked_account"
    assert _jobs(eval_clean) == []


async def test_the_shared_cluster_refuses_the_queueing(
    owner_engine: AsyncEngine, eval_db: EvalDb
) -> None:
    user_id = (await seed_accounts(owner_engine, (STEADY,)))[STEADY.name]
    with pytest.raises(SyntheticJobRefused) as refused:
        await _queue(owner_engine, user_id)
    assert refused.value.code == "shared_cluster"
    assert _jobs(eval_db) == []


async def test_a_schema_not_at_head_refuses_the_queueing(
    owner_engine: AsyncEngine, eval_db: EvalDb, alone_in_cluster: None
) -> None:
    user_id = (await seed_accounts(owner_engine, (STEADY,)))[STEADY.name]
    async with owner_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text("UPDATE alembic_version SET version_num = '0030_older'"))
            with pytest.raises(SyntheticJobRefused) as refused:
                await queue_run(conn, user_id, STEADY.name)
        finally:
            await trans.rollback()
    assert refused.value.code == "schema_not_at_head"
    assert _jobs(eval_db) == []


@pytest.mark.parametrize("plan", ["", "Steady", "a plan", "x" * 41, "steady;"])
async def test_an_invalid_plan_name_is_refused(
    owner_engine: AsyncEngine, alone_in_cluster: None, plan: str
) -> None:
    user_id = (await seed_accounts(owner_engine, (STEADY,)))[STEADY.name]
    with pytest.raises(SyntheticJobRefused) as refused:
        await _queue(owner_engine, user_id, plan)
    assert refused.value.code == "invalid_run"


async def test_an_account_id_that_is_not_a_uuid_is_refused(
    owner_engine: AsyncEngine, alone_in_cluster: None
) -> None:
    with pytest.raises(SyntheticJobRefused) as refused:
        await _queue(owner_engine, str(uuid4()))  # pyright: ignore[reportArgumentType]
    assert refused.value.code == "invalid_run"


async def test_the_start_check_as_the_app_role(
    owner_engine: AsyncEngine, factory: Any, eval_clean: EvalDb, alone_in_cluster: None
) -> None:
    await seed_accounts(owner_engine)
    assert await database_refusal(factory) is None
    await _person(eval_clean)
    assert await database_refusal(factory) == "unmarked_account"


async def test_the_start_check_refuses_the_shared_cluster(
    owner_engine: AsyncEngine, factory: Any
) -> None:
    await seed_accounts(owner_engine)
    assert await database_refusal(factory) == "shared_cluster"
