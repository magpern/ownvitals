"""The AI eval's synthetic accounts (ADR 0014 section 9; #531): ``users.synthetic`` (migration
0031), which only the owner role writes, and the seeder's refusals on the test database, whose
name does not end in ``_ai_eval``. The seed itself, on an ``_ai_eval`` database, is
``test_ai_eval_seed_db.py``.

Synthetic accounts and data only: addresses in ``ai-evaluation.invalid``, invented readings.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from ctx import make_context
from ownvitals.ai.eval_seed import seed_accounts
from ownvitals.ai.eval_seed_model import (
    PLANS,
    measure_groups,
    readings,
)
from ownvitals.auth.synthetic import (
    SyntheticRefused,
    create_synthetic_account,
    is_eval_database_name,
)
from ownvitals.config import Settings
from ownvitals.connections.synthetic import create_synthetic_connection
from ownvitals.db import create_engine as create_app_engine
from ownvitals.db import create_session_factory, user_session

SYNTHETIC = True
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
STEADY, SPARSE, PLANTED = PLANS
PERSON = "person@example.test"  # an unmarked account, as a sign-up would make it


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


# --- the column and its privileges ---------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO users (email, email_normalized, email_verified_at, synthetic) "
        "VALUES ('x@ai-evaluation.invalid', 'x@ai-evaluation.invalid', now(), true)",
        "INSERT INTO users (email, email_normalized, email_verified_at, synthetic) "
        "VALUES ('x@ai-evaluation.invalid', 'x@ai-evaluation.invalid', now(), false)",
        "UPDATE users SET synthetic = true",
        "UPDATE users SET synthetic = false",
    ],
)
def test_the_app_role_cannot_write_the_mark(
    clean_auth: None, app_database_url: str, statement: str
) -> None:
    engine = create_engine(app_database_url)
    try:
        with engine.connect() as conn, pytest.raises(DBAPIError) as raised:
            conn.execute(text(statement))
        assert getattr(raised.value.orig, "sqlstate", None) == "42501"  # insufficient_privilege
    finally:
        engine.dispose()


def test_a_sign_up_is_unmarked_and_the_app_role_still_writes_the_other_columns(
    clean_auth: None, app_database_url: str
) -> None:
    _app_insert_person(app_database_url)
    engine = create_engine(app_database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET locale = 'sv', disabled_at = now()"))
            assert conn.execute(text("SELECT synthetic FROM users")).scalars().all() == [False]
            conn.execute(text("DELETE FROM users"))
    finally:
        engine.dispose()


def test_the_owner_role_writes_the_mark(clean_auth: None, owner_database_url: str) -> None:
    engine = create_engine(owner_database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (email, email_normalized, email_verified_at, synthetic) "
                    "VALUES ('o@ai-evaluation.invalid', 'o@ai-evaluation.invalid', now(), true)"
                )
            )
            assert conn.execute(text("SELECT synthetic FROM users")).scalar_one() is True
            conn.execute(text("DELETE FROM users"))
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "email",
    [
        "person@example.test",
        "a@ai-evaluation.invalid.example",
        "a@AI-EVAL.invalid",
        " a@ai-evaluation.invalid",
        "@ai-evaluation.invalid",
        "a b@ai-evaluation.invalid",
        "a@b@ai-evaluation.invalid",
        "é@ai-evaluation.invalid",
        f"{'a' * 49}@ai-evaluation.invalid",
    ],
)
async def test_an_address_outside_the_reserved_domain_is_refused(
    clean_auth: None, owner_database_url: str, email: str
) -> None:
    engine = create_async_engine(owner_database_url)
    try:
        async with engine.begin() as conn:
            with pytest.raises(SyntheticRefused, match=r"ai-evaluation\.invalid"):
                await create_synthetic_account(conn, email)
    finally:
        await engine.dispose()
    assert _scalar(owner_database_url, "SELECT count(*) FROM users") == 0


# --- the database and the address ----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        ("ownvitals_ai_eval", True),
        ("x_ai_eval", True),
        ("_ai_eval", False),
        ("ownvitals", False),
        ("ownvitals_ai_eval_copy", False),
        ("ownvitals_AI_EVAL", False),
        ("ownvitals_ai_eval ", False),
    ],
)
def test_the_eval_database_name(name: str, allowed: bool) -> None:
    assert is_eval_database_name(name) is allowed


async def test_no_account_is_seeded_outside_an_eval_database(
    clean_auth: None, owner_database_url: str
) -> None:
    engine = create_async_engine(owner_database_url)
    try:
        with pytest.raises(SyntheticRefused, match="_ai_eval"):
            await seed_accounts(engine)
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one() == 0
    finally:
        await engine.dispose()


async def test_no_data_is_seeded_outside_an_eval_database(
    clean_auth: None, app_database_url: str, owner_database_url: str
) -> None:
    engine = create_engine(owner_database_url)
    try:
        with engine.begin() as conn:  # a marked account, as only the owner can make it
            user = conn.execute(
                text(
                    "INSERT INTO users (email, email_normalized, email_verified_at, synthetic) "
                    "VALUES ('s@ai-evaluation.invalid', 's@ai-evaluation.invalid', now(), true) "
                    "RETURNING id"
                )
            ).scalar_one()
    finally:
        engine.dispose()
    app = create_app_engine(Settings(database_url=app_database_url))  # pyright: ignore[reportArgumentType]
    try:
        ctx = make_context(user)
        with pytest.raises(SyntheticRefused):
            async with user_session(create_session_factory(app), ctx) as db:
                await create_synthetic_connection(db, ctx)
    finally:
        await app.dispose()
    assert _scalar(owner_database_url, "SELECT count(*) FROM connections") == 0


# --- the plans (pure) ----------------------------------------------------------------------


def test_the_plans_are_synthetic_and_distinct() -> None:
    assert len({plan.name for plan in PLANS}) == len(PLANS)
    assert all(plan.email.endswith("@ai-evaluation.invalid") for plan in PLANS)
    assert [plan.days for plan in PLANS] == [120, 2, 60]
    ids = [g.grpid for i, plan in enumerate(PLANS) for g in measure_groups(plan, i, NOW.date())]
    assert len(ids) == len(set(ids))
    days = readings(SPARSE, NOW.date())
    assert [r.day.isoformat() for r in days] == ["2026-11-01", "2026-11-02"]
