"""The AI eval's seeder on an ``_ai_eval`` database (ADR 0014 section 9; #531):
``ownvitals.auth.synthetic``, ``ownvitals.connections.synthetic`` and ``ownvitals.ai.eval_seed``.

The module's fixture creates ``<test database>_ai_eval`` and prepares it as the owner prepares
the eval's: the roles script, the migrations as the owner role, then the db-functions scripts as
the superuser. The function-owner roles may own objects in one database of a cluster only (each
db-functions script refuses otherwise), so this module needs the test database at base, as
every other module leaves it, and drops the eval database at its end.

Synthetic accounts and data only: addresses in ``ai-evaluation.invalid``, invented readings.
"""

import os
import secrets
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from conftest import RoleUrls
from contexttools import assert_isolated, owned_tables, plant, probe
from ctx import make_context
from dbtools import ROLES_SCRIPT, alembic_config, install_db_functions, unavailable, url_for
from ownvitals.ai import consent_text
from ownvitals.ai.eval_seed import seed_account_data, seed_accounts
from ownvitals.ai.eval_seed_model import (
    PLANS,
    PLANTED_DEVICE_TYPE,
    AccountPlan,
)
from ownvitals.auth.synthetic import (
    EVAL_DATABASE_SUFFIX,
    SyntheticRefused,
    account_is_synthetic,
    create_synthetic_account,
    unmarked_accounts,
)
from ownvitals.config import Settings
from ownvitals.context import UserContext
from ownvitals.db import SYSTEM_TABLES, create_session_factory, user_session
from ownvitals.db import create_engine as create_app_engine
from rls_guard import check_tables
from role_guard import check_roles

SYNTHETIC = True
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
STEADY, SPARSE, PLANTED = PLANS
PERSON = "person@example.test"  # an unmarked account, as a sign-up would make it


def _open_tx(factory: Any, ctx: UserContext) -> Callable[[], AbstractAsyncContextManager[Any]]:
    """The context's transaction opener, as a job's fence hands it to the seeder."""

    def open_tx() -> AbstractAsyncContextManager[Any]:
        return user_session(factory, ctx)

    return open_tx


@dataclass(frozen=True)
class EvalDb:
    superuser: str
    owner: str
    app: str


def _run_roles_script(url: str, role_urls: RoleUrls, tmp: Path) -> None:
    psql = shutil.which("psql")
    if psql is None:
        unavailable("psql is needed to run 10-roles.sql")
    assert psql is not None
    (tmp / "owner").write_text(make_url(role_urls.owner).password or "", "utf-8")
    (tmp / "app").write_text(make_url(role_urls.app).password or "", "utf-8")
    (tmp / "maint").write_text(secrets.token_hex(16), "utf-8")
    parsed = make_url(url)
    result = subprocess.run(  # noqa: S603 - our own script, fixed arguments
        [
            psql, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", parsed.host or "", "-p",
            str(parsed.port or 5432), "-U", parsed.username or "", "-d", parsed.database or "",
            "-f", str(ROLES_SCRIPT),
        ],
        env={
            **os.environ,
            "DATABASE_OWNER_PASSWORD_FILE": str(tmp / "owner"),
            "DATABASE_PASSWORD_FILE": str(tmp / "app"),
            "DATABASE_MAINT_PASSWORD_FILE": str(tmp / "maint"),
            "PGPASSWORD": parsed.password or "",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr


def _drop(superuser: str, name: str) -> None:
    engine = create_engine(superuser, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        engine.dispose()


@pytest.fixture(name="eval_db", scope="module")
def eval_db_fixture(
    role_urls: RoleUrls, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[EvalDb]:
    """``<test database>_ai_eval``, prepared as the owner prepares the eval's database."""
    main = make_url(role_urls.owner)
    name = f"{main.database}_ai_eval"
    superuser = make_url(os.environ["OWNVITALS_TEST_DATABASE_URL"])
    main_superuser = superuser.render_as_string(hide_password=False)
    _drop(main_superuser, name)
    engine = create_engine(main_superuser, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    eval_superuser = superuser.set(database=name).render_as_string(hide_password=False)
    db = EvalDb(
        superuser=eval_superuser,
        owner=url_for(eval_superuser, "ownvitals_owner", main.password or ""),
        app=url_for(eval_superuser, "ownvitals_app", make_url(role_urls.app).password or ""),
    )
    try:
        _run_roles_script(eval_superuser, role_urls, tmp_path_factory.mktemp("eval-roles"))
        patch = pytest.MonkeyPatch()
        patch.setenv("OWNVITALS_MIGRATIONS_DATABASE_URL", db.owner)
        try:
            command.upgrade(alembic_config(), "head")
        finally:
            patch.undo()
        install_db_functions(eval_superuser)
        yield db
    finally:
        _drop(main_superuser, name)


@pytest.fixture(name="eval_clean")
def eval_clean_fixture(eval_db: EvalDb) -> EvalDb:
    engine = create_engine(eval_db.superuser)
    try:
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE users CASCADE"))
    finally:
        engine.dispose()
    return eval_db


@pytest.fixture
async def owner_engine(eval_clean: EvalDb) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(eval_clean.owner)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def factory(eval_clean: EvalDb) -> AsyncIterator[object]:
    engine = create_app_engine(Settings(database_url=eval_clean.app))  # pyright: ignore[reportArgumentType]
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def _scalar(url: str, sql: str, **params: object) -> object:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def _app_insert_person(url: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (email, email_normalized, email_verified_at) "
                    "VALUES (:e, :e, now())"
                ),
                {"e": PERSON},
            )
    finally:
        engine.dispose()


# --- seeding the accounts ------------------------------------------------------------------


async def test_the_seeded_accounts_are_marked_and_a_second_seed_reuses_them(
    owner_engine: AsyncEngine, eval_clean: EvalDb
) -> None:
    first = await seed_accounts(owner_engine)
    assert set(first) == {plan.name for plan in PLANS}
    assert await seed_accounts(owner_engine) == first
    rows = _scalar(
        eval_clean.superuser,
        "SELECT array_agg(email ORDER BY email) FILTER (WHERE synthetic) FROM users",
    )
    assert rows == sorted(plan.email for plan in PLANS)
    async with owner_engine.connect() as conn:
        assert await unmarked_accounts(conn) == 0
        for user in first.values():
            assert await account_is_synthetic(conn, user)


async def test_the_app_role_cannot_seed_an_account(eval_clean: EvalDb) -> None:
    engine = create_async_engine(eval_clean.app)
    try:
        with pytest.raises(DBAPIError) as raised:
            async with engine.begin() as conn:
                await create_synthetic_account(conn, STEADY.email)
        assert getattr(raised.value.orig, "sqlstate", None) == "42501"
    finally:
        await engine.dispose()
    assert _scalar(eval_clean.superuser, "SELECT count(*) FROM users") == 0


async def test_an_unmarked_account_stops_the_seed(
    owner_engine: AsyncEngine, eval_clean: EvalDb
) -> None:
    await seed_accounts(owner_engine, (STEADY,))
    _app_insert_person(eval_clean.app)
    async with owner_engine.connect() as conn:
        assert await unmarked_accounts(conn) == 1
    with pytest.raises(SyntheticRefused, match="unmarked"):
        await seed_accounts(owner_engine)
    # One transaction: nothing of the refused seed was written.
    assert _scalar(eval_clean.superuser, "SELECT count(*) FROM users WHERE synthetic") == 1


async def test_the_accounts_seed_in_one_transaction(
    owner_engine: AsyncEngine, eval_clean: EvalDb
) -> None:
    bad = AccountPlan("not allowed", 1, 70.0, 60)  # a space: refused after the first plan
    with pytest.raises(SyntheticRefused):
        await seed_accounts(owner_engine, (STEADY, bad))
    assert _scalar(eval_clean.superuser, "SELECT count(*) FROM users") == 0


# --- seeding the data ----------------------------------------------------------------------


async def test_the_data_seed_plants_each_plan(
    owner_engine: AsyncEngine, factory: object, eval_clean: EvalDb
) -> None:
    accounts = await seed_accounts(owner_engine)
    settings = Settings(database_url=eval_clean.app)  # pyright: ignore[reportArgumentType]
    assert settings.ai_insights is False  # the seeder needs no switch (ADR 0014 section 9)
    for plan in PLANS:
        ctx = make_context(accounts[plan.name])
        await seed_account_data(factory, ctx, _open_tx(factory, ctx), settings, plan, now=NOW)  # pyright: ignore[reportArgumentType]
        await seed_account_data(factory, ctx, _open_tx(factory, ctx), settings, plan, now=NOW)  # pyright: ignore[reportArgumentType]
    su = eval_clean.superuser
    for plan in PLANS:
        owner = accounts[plan.name]
        assert _scalar(su, "SELECT count(*) FROM measure_groups WHERE owner_id = :o", o=owner) == (
            2 * plan.days
        )
        assert (
            _scalar(
                su,
                "SELECT count(*) FROM connections WHERE owner_id = :o AND provider = 'test' "
                "AND status = 'disconnected' AND secret IS NULL AND key_id IS NULL",
                o=owner,
            )
            == 1
        )
        assert _scalar(su, "SELECT type FROM devices WHERE owner_id = :o", o=owner) == (
            plan.device_type
        )
        assert (
            _scalar(
                su,
                "SELECT count(*) FROM ai_consents WHERE owner_id = :o AND withdrawn_at IS NULL "
                "AND consent_version = :v",
                o=owner,
                v=consent_text.CONSENT_VERSION,
            )
            == 1
        )
        weights = _scalar(  # the normalisation ran: one weight observation per reading
            su,
            "SELECT count(*) FROM observations WHERE owner_id = :o AND metric_key = 'weight'",
            o=owner,
        )
        assert weights == plan.days
    assert _scalar(su, "SELECT count(*) FROM devices WHERE type = :t", t=PLANTED_DEVICE_TYPE) == 1


async def test_the_data_seed_refuses_an_unmarked_account(
    factory: object, eval_clean: EvalDb
) -> None:
    _app_insert_person(eval_clean.app)
    person = _scalar(eval_clean.superuser, "SELECT id FROM users")
    assert isinstance(person, UUID)
    settings = Settings(database_url=eval_clean.app)  # pyright: ignore[reportArgumentType]
    ctx = make_context(person)
    with pytest.raises(SyntheticRefused):
        await seed_account_data(factory, ctx, _open_tx(factory, ctx), settings, STEADY, now=NOW)  # pyright: ignore[reportArgumentType]
    assert _scalar(eval_clean.superuser, "SELECT count(*) FROM connections") == 0
    assert _scalar(eval_clean.superuser, "SELECT count(*) FROM measure_groups") == 0


# --- the guards in the eval database -------------------------------------------------------


def test_the_role_and_rls_guards_pass_in_the_eval_database(eval_clean: EvalDb) -> None:
    engine = create_engine(eval_clean.superuser)
    try:
        with engine.connect() as conn:
            # The one finding is the cluster's, not the eval database's: this test shares its
            # cluster with the test database, which the app role may also connect to. A real
            # eval runs on a cluster of its own (see the PR's notes), where the list is empty.
            name = make_url(eval_clean.superuser).database or ""
            main = name.removesuffix(EVAL_DATABASE_SUFFIX)
            assert check_roles(conn) == [f"ownvitals_app must not connect to {[main]}"]
            assert check_tables(conn, SYSTEM_TABLES) == []
    finally:
        engine.dispose()


async def test_the_seed_context_sees_and_writes_only_its_own_rows(
    owner_engine: AsyncEngine, factory: object, eval_clean: EvalDb
) -> None:
    """RLS in the seeder's execution context: the app role on the eval database, in the
    session of a context for one seeded account, probed against the other's rows."""
    accounts = await seed_accounts(owner_engine, (STEADY, SPARSE))
    me = plant(eval_clean.superuser, accounts[STEADY.name])
    other = plant(eval_clean.superuser, accounts[SPARSE.name])
    tables = owned_tables(eval_clean.superuser)
    async with user_session(factory, make_context(me.user)) as db:  # pyright: ignore[reportArgumentType]
        report = await probe(db, me, other, tables)
    assert_isolated(report, me, tables)
