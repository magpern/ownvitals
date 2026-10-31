"""The app role's engine, session factory and readiness query. The pool's reset (``DISCARD
ALL``) and the per-checkout check are in ``test_isolation.py``, as the app role across users."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ownvitals.config import Settings
from ownvitals.db import check_ready, create_engine, create_session_factory


def settings_for(url: str) -> Settings:
    return Settings(database_url=url)  # pyright: ignore[reportArgumentType]


def test_engine_is_lazy_and_uses_psycopg(unreachable_database_url: str) -> None:
    engine = create_engine(settings_for(unreachable_database_url))  # would raise if it connected
    assert engine.url.drivername == "postgresql+psycopg"
    assert "not-a-real-password" not in repr(engine.url)


async def test_session_factory_runs_a_query(app_database_url: str) -> None:
    engine = create_engine(settings_for(app_database_url))
    try:
        async with create_session_factory(engine)() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()


async def test_check_ready_passes_against_a_database_and_raises_without_one(
    app_database_url: str, unreachable_database_url: str
) -> None:
    engine = create_engine(settings_for(app_database_url))
    try:
        await check_ready(create_session_factory(engine))
    finally:
        await engine.dispose()
    engine = create_engine(settings_for(unreachable_database_url))
    try:
        with pytest.raises(DBAPIError):
            await check_ready(create_session_factory(engine))
    finally:
        await engine.dispose()


async def test_statement_errors_do_not_carry_the_bound_parameters(
    app_database_url: str,
) -> None:
    engine = create_engine(settings_for(app_database_url))
    try:
        async with create_session_factory(engine)() as session:
            with pytest.raises(DBAPIError) as excinfo:
                await session.execute(
                    text("SELECT * FROM no_such_table WHERE note = :note"),
                    {"note": "planted-parameter-value"},
                )
        assert "planted-parameter-value" not in str(excinfo.value)
    finally:
        await engine.dispose()
