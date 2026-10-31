"""Guards 3 and 4 of ADR 0002 section 7, on test-only owned tables, as the app role."""

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import AsyncAdaptedQueuePool

from ctx import make_context
from ownvitals.config import Settings
from ownvitals.context import UserContext, issuer_token
from ownvitals.db import (
    TransactionOwnedError,
    create_engine,
    create_migrations_engine,
    create_session_factory,
    system_session,
    user_session,
)

pytestmark = pytest.mark.usefixtures("owned_tables")

A = make_context()
B = make_context()
Sessions = async_sessionmaker[AsyncSession]
INSERT_PARENT = text("INSERT INTO t_parent (owner_id, name) VALUES (:o, :n) RETURNING id")
INSERT_CHILD = text("INSERT INTO t_child (owner_id, parent_id) VALUES (:o, :p)")


def settings_for(app_url: str) -> Settings:
    return Settings(database_url=app_url, environment="test")  # pyright: ignore[reportArgumentType]


@pytest.fixture
async def sessions(app_database_url: str) -> AsyncIterator[Sessions]:
    engine = create_engine(settings_for(app_database_url))
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def add_parent(sessions: Sessions, ctx: UserContext, name: str) -> uuid.UUID:
    async with user_session(sessions, ctx) as session:
        result = await session.execute(INSERT_PARENT, {"o": ctx.user_id, "n": name})
        return result.scalar_one()


async def names(sessions: Sessions, ctx: UserContext) -> list[str]:
    async with user_session(sessions, ctx) as session:
        return sorted((await session.execute(text("SELECT name FROM t_parent"))).scalars())


async def test_a_never_sees_bs_rows(sessions: Sessions) -> None:
    await add_parent(sessions, A, "a1")
    await add_parent(sessions, B, "b1")
    assert await names(sessions, A) == ["a1"]
    assert await names(sessions, B) == ["b1"]


async def test_a_cannot_update_or_delete_bs_rows(sessions: Sessions) -> None:
    b_id = await add_parent(sessions, B, "b1")
    async with user_session(sessions, A) as session:
        updated = await session.execute(
            text("UPDATE t_parent SET name = 'x' WHERE id = :i"), {"i": b_id}
        )
        deleted = await session.execute(text("DELETE FROM t_parent WHERE id = :i"), {"i": b_id})
        assert (updated.rowcount, deleted.rowcount) == (0, 0)  # pyright: ignore[reportAttributeAccessIssue]
    assert await names(sessions, B) == ["b1"]


async def test_a_cannot_insert_a_row_for_b(sessions: Sessions) -> None:
    with pytest.raises(DBAPIError, match="row-level security"):
        async with user_session(sessions, A) as session:
            await session.execute(INSERT_PARENT, {"o": B.user_id, "n": "x"})
    assert await names(sessions, B) == []


async def test_a_cannot_move_a_row_to_b(sessions: Sessions) -> None:
    await add_parent(sessions, A, "a1")
    with pytest.raises(DBAPIError, match="row-level security"):
        async with user_session(sessions, A) as session:
            await session.execute(text("UPDATE t_parent SET owner_id = :o"), {"o": B.user_id})
    assert await names(sessions, A) == ["a1"]


async def test_a_cannot_create_a_child_of_bs_parent(sessions: Sessions) -> None:
    b_parent = await add_parent(sessions, B, "b1")
    first_lines = []
    for parent_id in (b_parent, uuid.uuid4()):  # B's real parent, and one that does not exist
        with pytest.raises(IntegrityError) as excinfo:
            async with user_session(sessions, A) as session:
                await session.execute(INSERT_CHILD, {"o": A.user_id, "p": parent_id})
        first_lines.append(str(excinfo.value.orig).splitlines()[0])
    # A cannot tell B's ID from a made-up one: the same constraint error either way.
    assert "t_child_owner_id_parent_id_fkey" in first_lines[0]
    assert first_lines[0] == first_lines[1]
    # With B's owner_id on the child, RLS refuses the row.
    with pytest.raises(DBAPIError, match="row-level security"):
        async with user_session(sessions, A) as session:
            await session.execute(INSERT_CHILD, {"o": B.user_id, "p": b_parent})


async def test_a_child_of_its_own_parent_works_and_is_private(sessions: Sessions) -> None:
    a_parent = await add_parent(sessions, A, "a1")
    async with user_session(sessions, A) as session:
        await session.execute(INSERT_CHILD, {"o": A.user_id, "p": a_parent})
    async with user_session(sessions, B) as session:
        assert (await session.execute(text("SELECT count(*) FROM t_child"))).scalar_one() == 0


async def test_a_session_without_a_context_sees_no_rows(sessions: Sessions) -> None:
    await add_parent(sessions, A, "a1")
    async with system_session(sessions) as session:
        assert (await session.execute(text("SELECT count(*) FROM t_parent"))).scalar_one() == 0
    async with sessions() as session:  # the helper was never used on this connection
        assert (await session.execute(text("SELECT count(*) FROM t_parent"))).scalar_one() == 0
    with pytest.raises(DBAPIError, match="row-level security"):
        async with system_session(sessions) as session:
            await session.execute(INSERT_PARENT, {"o": A.user_id, "n": "x"})


async def test_a_unique_value_of_b_cannot_be_detected_or_blocked_by_a(sessions: Sessions) -> None:
    await add_parent(sessions, B, "taken")
    await add_parent(sessions, A, "taken")  # not blocked, so A learns nothing about B
    assert await names(sessions, A) == ["taken"]


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE t_parent DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE t_parent NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE t_parent OWNER TO ownvitals_app",
        "DROP POLICY owner_isolation ON t_parent",
        "CREATE POLICY open ON t_parent USING (true)",
        "ALTER TABLE t_parent ADD COLUMN x int",
        "DROP TABLE t_parent",
        "TRUNCATE t_parent",
        "CREATE TABLE t_new (id int)",
        "CREATE TEMP TABLE t_tmp (id int)",
        "SET ROLE ownvitals_owner",
        "CREATE SCHEMA s",
        "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    ],
)
async def test_the_app_role_cannot_change_the_schema(sessions: Sessions, statement: str) -> None:
    with pytest.raises(ProgrammingError, match=r"permission denied|must be owner"):
        async with user_session(sessions, A) as session:
            await session.execute(text(statement))


async def test_the_owner_is_also_bound_by_the_policy(owner_database_url: str) -> None:
    settings = Settings(migrations_database_url=owner_database_url, environment="test")  # pyright: ignore[reportArgumentType]
    engine = create_migrations_engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.user_id', :u, true)"), {"u": str(A.user_id)}
            )
            await connection.execute(INSERT_PARENT, {"o": A.user_id, "n": "x"})
        async with engine.begin() as connection:  # FORCE: no setting, no rows, even the owner
            count = (await connection.execute(text("SELECT count(*) FROM t_parent"))).scalar_one()
            assert count == 0
    finally:
        await engine.dispose()


async def test_a_pooled_connection_carries_no_user_id_afterwards(app_database_url: str) -> None:
    engine = create_engine(
        settings_for(app_database_url),
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
    )
    sessions = create_session_factory(engine)
    try:
        await add_parent(sessions, A, "a1")
        async with sessions() as session:
            pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            setting = (
                await session.execute(text("SELECT current_setting('app.user_id', true)"))
            ).scalar_one()
            count = (await session.execute(text("SELECT count(*) FROM t_parent"))).scalar_one()
        async with user_session(sessions, A) as session:
            same = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one() == pid
        assert same, "the test must reuse the same connection"
        assert setting in (None, "")
        assert count == 0
    finally:
        await engine.dispose()


async def test_commit_and_rollback_inside_a_session_raise(sessions: Sessions) -> None:
    for manager in (user_session(sessions, A), system_session(sessions)):
        async with manager as session:
            with pytest.raises(TransactionOwnedError):
                await session.commit()
            with pytest.raises(TransactionOwnedError):
                await session.rollback()
            with pytest.raises(TransactionOwnedError):
                await session.run_sync(lambda sync_session: sync_session.commit())
    # The refusals leave the transaction intact: it still works and commits at the end.
    a_id = await add_parent(sessions, A, "kept")
    async with user_session(sessions, A) as session:
        with pytest.raises(TransactionOwnedError):
            await session.commit()
        await session.execute(
            text("UPDATE t_parent SET name = 'changed' WHERE id = :i"), {"i": a_id}
        )
    assert await names(sessions, A) == ["changed"]


async def test_an_exception_rolls_back_the_whole_session(sessions: Sessions) -> None:
    async def failing() -> None:
        async with user_session(sessions, A) as session:
            await session.execute(INSERT_PARENT, {"o": A.user_id, "n": "x"})
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await failing()
    assert await names(sessions, A) == []


async def test_a_raw_user_id_is_not_a_context(sessions: Sessions) -> None:
    with pytest.raises(TypeError, match="UserContext"):
        async with user_session(sessions, A.user_id):  # pyright: ignore[reportArgumentType]
            pytest.fail("a raw user id must not open a session")


async def test_savepoints_work_and_keep_the_user_id(sessions: Sessions) -> None:
    async with user_session(sessions, A) as session:
        await session.execute(INSERT_PARENT, {"o": A.user_id, "n": "kept"})
        with pytest.raises(IntegrityError):  # the "insert, catch the unique violation" pattern
            async with session.begin_nested():
                await session.execute(INSERT_PARENT, {"o": A.user_id, "n": "kept"})
        user = (await session.execute(text("SELECT current_setting('app.user_id')"))).scalar_one()
        assert user == str(A.user_id)  # survived the rolled-back savepoint
        async with session.begin_nested():  # a savepoint that ends normally is fine too
            await session.execute(INSERT_PARENT, {"o": A.user_id, "n": "second"})
        with pytest.raises(TransactionOwnedError):  # but the transaction is still not ours to end
            await session.commit()
        assert (await session.execute(text("SELECT count(*) FROM t_parent"))).scalar_one() == 2
    assert await names(sessions, A) == ["kept", "second"]


async def test_rolling_back_through_another_path_fails_closed(sessions: Sessions) -> None:
    async with user_session(sessions, A) as session:
        await session.execute(INSERT_PARENT, {"o": A.user_id, "n": "x"})
        await session.run_sync(lambda sync_session: sync_session.rollback())
        # The transaction (and with it app.user_id) is gone: no rows, and no owned writes.
        assert (await session.execute(text("SELECT count(*) FROM t_parent"))).scalar_one() == 0
        with pytest.raises(DBAPIError, match="row-level security"):
            await session.execute(INSERT_PARENT, {"o": A.user_id, "n": "y"})


class _Sub(UserContext):
    pass


@pytest.mark.parametrize(
    "fake",
    [
        SimpleNamespace(user_id=B.user_id, role="user"),
        _Sub(B.user_id, "user", issuer=issuer_token()),
        B.user_id,
        {"user_id": B.user_id},
        None,
    ],
    ids=["look-alike", "subclass instance", "bare uuid", "dict", "none"],
)
async def test_only_the_exact_user_context_opens_a_session(
    sessions: Sessions, fake: object
) -> None:
    await add_parent(sessions, B, "b1")
    with pytest.raises(TypeError, match="UserContext is required"):
        async with user_session(sessions, fake):  # pyright: ignore[reportArgumentType]
            pytest.fail("a session must not open for anything but a UserContext")


async def test_a_pooled_connection_with_leftover_session_state_is_replaced(
    app_database_url: str,
) -> None:
    # The checkout check is the second line of defence, behind DISCARD ALL: switch the reset off
    # to prove that a connection that still carries state is replaced.
    engine = create_engine(
        settings_for(app_database_url),
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
    )
    for listener in list(engine.sync_engine.pool.dispatch.reset):
        event.remove(engine.sync_engine, "reset", listener)  # also from SQLAlchemy's registry

    @event.listens_for(engine.sync_engine, "reset")
    def rollback_only(dbapi_connection: Any, _record: object, reset_state: Any) -> None:
        # What a real engine does without DISCARD ALL: the pool's own rollback on return.
        if not (reset_state.terminate_only or not reset_state.asyncio_safe):
            dbapi_connection.rollback()

    sessions = create_session_factory(engine)
    try:
        await add_parent(sessions, B, "b1")
        for leftover in ("SET statement_timeout = 0", f"SET app.user_id = '{B.user_id}'"):
            async with sessions() as session:
                first_pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                await session.execute(text(leftover))  # session-level, not transaction-local
                await session.commit()  # the SET now outlives the transaction
            async with sessions() as session:  # the pool must not hand that connection out
                row = (
                    await session.execute(
                        text(
                            "SELECT pg_backend_pid(), current_setting('statement_timeout'), "
                            "current_setting('app.user_id', true), "
                            "(SELECT count(*) FROM t_parent)"
                        )
                    )
                ).one()
            assert row[0] != first_pid
            assert row[1] == "30s"
            assert row[2] in (None, "")
            assert row[3] == 0
    finally:
        await engine.dispose()


async def held_cursor_read_by_a(engine: AsyncEngine) -> list[str]:
    """B opens a WITH HOLD cursor over B's rows; then A, on the same connection, fetches it."""
    sessions = create_session_factory(engine)
    await add_parent(sessions, B, "b1")
    async with user_session(sessions, B) as session:
        first_pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        await session.execute(text("DECLARE leak CURSOR WITH HOLD FOR SELECT name FROM t_parent"))
    async with user_session(sessions, A) as session:
        assert (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one() == first_pid
        return list((await session.execute(text("FETCH ALL FROM leak"))).scalars())


async def test_a_holdable_cursor_survives_the_return_without_the_reset(
    app_database_url: str,
) -> None:
    # Documents the hole DISCARD ALL closes: B's held cursor, fetched by A.
    engine = create_engine(
        settings_for(app_database_url),
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
    )
    for listener in list(engine.sync_engine.pool.dispatch.reset):
        event.remove(engine.sync_engine, "reset", listener)  # also from SQLAlchemy's registry

    @event.listens_for(engine.sync_engine, "reset")
    def rollback_only(dbapi_connection: Any, _record: object, reset_state: Any) -> None:
        # What a real engine does without DISCARD ALL: the pool's own rollback on return.
        if not (reset_state.terminate_only or not reset_state.asyncio_safe):
            dbapi_connection.rollback()

    try:
        assert await held_cursor_read_by_a(engine) == ["b1"]
    finally:
        await engine.dispose()


async def test_a_holdable_cursor_does_not_survive_the_return(app_database_url: str) -> None:
    engine = create_engine(
        settings_for(app_database_url),
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
    )
    try:
        with pytest.raises(DBAPIError, match='cursor "leak" does not exist'):
            await held_cursor_read_by_a(engine)
    finally:
        await engine.dispose()


PREPARE_THRESHOLD = 5  # psycopg's default: the 6th run of the same text is prepared
PREPARED_NAMES = text("SELECT name FROM pg_prepared_statements WHERE left(name, 5) = '_pg3_'")


async def run_until_prepared(session: AsyncSession, ctx: UserContext) -> int:
    """Run one statement more often than psycopg's threshold; return the backend pid."""
    pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    for _ in range(PREPARE_THRESHOLD + 2):
        await session.execute(
            text("SELECT count(*) FROM t_parent WHERE owner_id = :owner"),
            {"owner": ctx.user_id},
        )
    # psycopg prepared it server side, on this backend.
    assert (await session.execute(PREPARED_NAMES)).scalars().all(), "nothing was prepared"
    return pid


async def test_the_reset_keeps_one_connection_working_across_many_uses(
    app_database_url: str,
) -> None:
    # Past psycopg's prepare threshold the same statement is prepared server side; DISCARD ALL
    # drops it, and psycopg must notice instead of failing with "prepared statement ... does
    # not exist" (see discard_session_state).
    engine = create_engine(
        settings_for(app_database_url),
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
    )
    sessions = create_session_factory(engine)
    try:
        pids = set()
        for i in range(4):
            await add_parent(sessions, A, f"a{i}")
            async with user_session(sessions, A) as session:
                pids.add(await run_until_prepared(session, A))
            # Returned to the pool and reset: the server holds no prepared statement now.
            async with user_session(sessions, A) as session:
                pids.add((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
                assert not (await session.execute(PREPARED_NAMES)).scalars().all()
                assert (
                    await session.execute(text("SELECT count(*) FROM t_parent"))
                ).scalar_one() == i + 1
        assert len(pids) == 1, "the test must reuse the same connection"
    finally:
        await engine.dispose()


async def test_the_reset_clears_temp_tables_and_session_settings(app_database_url: str) -> None:
    engine = create_engine(
        settings_for(app_database_url),
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
    )
    sessions = create_session_factory(engine)
    try:
        async with sessions() as session:
            pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            # TEMP is revoked from the app role: the settings are what it can leave behind.
            await session.execute(text("SET app.note = 'left'"))
            await session.commit()
        async with sessions() as session:
            row = (
                await session.execute(
                    text("SELECT pg_backend_pid(), current_setting('app.note', true)")
                )
            ).one()
        assert row[0] == pid
        assert row[1] in (None, "")
    finally:
        await engine.dispose()
