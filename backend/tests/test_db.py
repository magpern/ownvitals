from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from ownvitals.config import Settings
from ownvitals.db import check_ready, create_engine, create_session_factory


def settings_for(url: str) -> Settings:
    return Settings(database_url=url)  # pyright: ignore[reportArgumentType]


def one_connection_engine(url: str) -> AsyncEngine:
    """An engine whose pool holds exactly one connection, so every use is a reuse."""
    return create_engine(
        settings_for(url), poolclass=AsyncAdaptedQueuePool, pool_size=1, max_overflow=0
    )


def test_engine_is_lazy_and_uses_psycopg(unreachable_database_url: str) -> None:
    engine = create_engine(settings_for(unreachable_database_url))  # would raise if it connected
    assert engine.url.drivername == "postgresql+psycopg"
    assert "not-a-real-password" not in repr(engine.url)


async def test_session_factory_runs_a_query(database_url: str) -> None:
    engine = create_engine(settings_for(database_url))
    try:
        async with create_session_factory(engine)() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()


async def test_check_ready_passes_against_a_database_and_raises_without_one(
    database_url: str, unreachable_database_url: str
) -> None:
    engine = create_engine(settings_for(database_url))
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


async def test_statement_errors_do_not_carry_the_bound_parameters(database_url: str) -> None:
    engine = create_engine(settings_for(database_url))
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


# --- DISCARD ALL when a connection goes back to the pool ---------------------------------------


async def held_cursor_read_by_next_user(engine: AsyncEngine) -> list[str]:
    """User one declares a cursor WITH HOLD and returns the connection; user two (the same
    backend: a pool of one) reads from it."""
    factory = create_session_factory(engine)
    async with factory() as session:
        first_pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        await session.execute(text("DECLARE leak CURSOR WITH HOLD FOR SELECT 'one'"))
        await session.commit()
    async with factory() as session:
        assert (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one() == first_pid
        return list((await session.execute(text("FETCH ALL FROM leak"))).scalars())


async def test_a_holdable_cursor_survives_the_return_without_the_reset(
    database_url: str,
) -> None:
    # Documents the hole DISCARD ALL closes: with only the pool's own rollback, the next user of
    # the connection still reads the first user's cursor.
    engine = one_connection_engine(database_url)
    for listener in list(engine.sync_engine.pool.dispatch.reset):
        event.remove(engine.sync_engine, "reset", listener)  # also from SQLAlchemy's registry

    @event.listens_for(engine.sync_engine, "reset")
    def rollback_only(dbapi_connection: Any, _record: object, reset_state: Any) -> None:
        if not (reset_state.terminate_only or not reset_state.asyncio_safe):
            dbapi_connection.rollback()

    try:
        assert await held_cursor_read_by_next_user(engine) == ["one"]
    finally:
        await engine.dispose()


async def test_a_holdable_cursor_does_not_survive_the_return(database_url: str) -> None:
    engine = one_connection_engine(database_url)
    try:
        with pytest.raises(DBAPIError, match='cursor "leak" does not exist'):
            await held_cursor_read_by_next_user(engine)
    finally:
        await engine.dispose()


PREPARE_THRESHOLD = 5  # psycopg's default: the 6th run of the same text is prepared
PREPARED_NAMES = text("SELECT name FROM pg_prepared_statements WHERE left(name, 5) = '_pg3_'")


async def test_the_reset_keeps_one_connection_working_across_many_uses(database_url: str) -> None:
    # Past psycopg's prepare threshold the same statement is prepared server side; DISCARD ALL
    # drops it, and psycopg must notice instead of failing with "prepared statement ... does
    # not exist" (see discard_session_state).
    engine = one_connection_engine(database_url)
    factory = create_session_factory(engine)
    try:
        pids: set[int] = set()
        for round_ in range(6):
            async with factory() as session:
                pids.add((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
                for _ in range(PREPARE_THRESHOLD + 2):
                    await session.execute(text("SELECT :n + 1"), {"n": round_})
                assert (await session.execute(PREPARED_NAMES)).scalars().all(), "nothing prepared"
            # Returned to the pool and reset: the server holds no prepared statement now.
            async with factory() as session:
                pids.add((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
                assert not (await session.execute(PREPARED_NAMES)).scalars().all()
        assert len(pids) == 1, "the test must reuse the same connection"
    finally:
        await engine.dispose()


async def test_the_reset_clears_session_settings_and_temp_tables(database_url: str) -> None:
    engine = one_connection_engine(database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await session.execute(text("SET app.note = 'left'"))
            await session.execute(text("CREATE TEMP TABLE left_behind (x int)"))
            await session.commit()
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT pg_backend_pid(), current_setting('app.note', true), "
                        "to_regclass('pg_temp.left_behind')"
                    )
                )
            ).one()
        assert row[0] == pid
        assert row[1] in (None, "")
        assert row[2] is None
    finally:
        await engine.dispose()
