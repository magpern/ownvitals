"""Guard 6 of ADR 0002 section 7: web and worker refuse to run as a role that bypasses RLS.

The worker does not exist yet: #9 adds its startup to these tests (it uses the same
``create_engine`` and ``verify_database_role``)."""

import asyncio
import secrets
import socket
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text

from dbtools import url_for
from ownvitals.config import Settings
from ownvitals.db import DatabaseRoleError, create_engine, verify_database_role
from ownvitals.db.role_check import (
    EXPECTED_ROLE_SETTINGS,
    PROBLEMS,
    SETTINGS_PROBLEM,
    problems_for,
)
from ownvitals.web import create_app

PASSWORD = secrets.token_hex(8)
PROBE_ROLES = (
    "guard_super",
    "guard_bypass",
    "guard_member",
    "guard_table_owner",
    "guard_prog",
    "guard_read",
    "guard_write",
    "guard_schema",
    "guard_plain",
)


@pytest.fixture
def probe_roles(database_url: str) -> Iterator[None]:
    """Login roles that each break one rule, plus one that is fine."""
    engine = create_sync_engine(database_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        db = connection.execute(text("SELECT current_database()")).scalar_one()

        def run(*statements: str) -> None:
            for statement in statements:
                connection.execute(text(statement))

        def cleanup() -> None:
            run("DROP TABLE IF EXISTS t_probe_owned", "DROP SCHEMA IF EXISTS guard_s CASCADE")
            for role in (*PROBE_ROLES, "guard_target"):
                exists = connection.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
                ).first()
                if exists:
                    run(f"DROP OWNED BY {role}", f"DROP ROLE {role}")

        cleanup()
        run(
            f"CREATE ROLE guard_super LOGIN SUPERUSER PASSWORD '{PASSWORD}'",
            f"CREATE ROLE guard_bypass LOGIN BYPASSRLS PASSWORD '{PASSWORD}'",
            "CREATE ROLE guard_target NOLOGIN BYPASSRLS",
            f"CREATE ROLE guard_member LOGIN NOINHERIT PASSWORD '{PASSWORD}'",
            "GRANT guard_target TO guard_member",
            f"CREATE ROLE guard_table_owner LOGIN PASSWORD '{PASSWORD}'",
            "CREATE TABLE t_probe_owned (id int)",
            "ALTER TABLE t_probe_owned OWNER TO guard_table_owner",
            f"CREATE ROLE guard_prog LOGIN PASSWORD '{PASSWORD}'",
            "GRANT pg_execute_server_program TO guard_prog",
            f"CREATE ROLE guard_read LOGIN PASSWORD '{PASSWORD}'",
            "GRANT pg_read_server_files TO guard_read",
            f"CREATE ROLE guard_write LOGIN PASSWORD '{PASSWORD}'",
            "GRANT pg_write_server_files TO guard_write",
            f"CREATE ROLE guard_schema LOGIN PASSWORD '{PASSWORD}'",
            "CREATE SCHEMA guard_s AUTHORIZATION guard_schema",
            f"CREATE ROLE guard_plain LOGIN PASSWORD '{PASSWORD}'",
            "ALTER ROLE guard_plain SET statement_timeout = '30s'",
            "ALTER ROLE guard_plain SET lock_timeout = '5s'",
            "ALTER ROLE guard_plain SET idle_in_transaction_session_timeout = '60s'",
        )
        for role in PROBE_ROLES:
            run(f"GRANT CONNECT ON DATABASE {db} TO {role}")
        try:
            yield
        finally:
            cleanup()
    engine.dispose()


def settings_for(database_url: str, role: str, **overrides: object) -> Settings:
    url = url_for(database_url, role, PASSWORD)
    values: dict[str, object] = {"database_url": url, "environment": "test", **overrides}
    return Settings(**values)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("role", "problem"),
    [
        ("guard_super", "is a superuser"),
        ("guard_bypass", "has BYPASSRLS"),
        ("guard_member", "member of a superuser or BYPASSRLS role"),
        ("guard_table_owner", "owns tables"),
        ("guard_prog", "pg_execute_server_program"),
        ("guard_read", "pg_read_server_files"),
        ("guard_write", "pg_write_server_files"),
        ("guard_schema", "owns a schema"),
    ],
)
async def test_startup_and_every_connection_refuse_a_privileged_role(
    database_url: str, probe_roles: None, role: str, problem: str
) -> None:
    settings = settings_for(database_url, role)
    engine = create_engine(settings)
    try:
        with pytest.raises(DatabaseRoleError, match=problem) as startup:
            await verify_database_role(engine)
        with pytest.raises(DatabaseRoleError, match=problem):
            async with engine.connect() as connection:  # the per-connection check
                await connection.execute(text("SELECT 1"))
        assert PASSWORD not in str(startup.value)
    finally:
        await engine.dispose()


async def test_a_role_that_is_fine_passes(database_url: str, probe_roles: None) -> None:
    settings = settings_for(database_url, "guard_plain")
    engine = create_engine(settings)
    try:
        await verify_database_role(engine)
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()


async def test_the_app_role_passes(app_database_url: str) -> None:
    settings = Settings(database_url=app_database_url, environment="test")  # pyright: ignore[reportArgumentType]
    engine = create_engine(settings)
    try:
        await verify_database_role(engine)
    finally:
        await engine.dispose()


async def test_the_owner_role_is_refused_for_web_and_worker(owner_database_url: str) -> None:
    settings = Settings(database_url=owner_database_url, environment="test")  # pyright: ignore[reportArgumentType]
    engine = create_engine(settings)
    try:
        with pytest.raises(DatabaseRoleError, match=r"owns tables|owns the database"):
            await verify_database_role(engine)
    finally:
        await engine.dispose()


async def test_there_is_no_override_not_even_in_dev(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OWNVITALS_ALLOW_PRIVILEGED_DATABASE_ROLE", "true")  # ignored setting
    settings = Settings(database_url=database_url, environment="dev")  # pyright: ignore[reportArgumentType]
    engine = create_engine(settings)
    try:
        with pytest.raises(DatabaseRoleError, match="is a superuser"):
            await verify_database_role(engine)
    finally:
        await engine.dispose()


async def test_a_database_that_is_down_at_startup_does_not_stop_the_app_but_stays_checked(
    unreachable_database_url: str,
) -> None:
    settings = Settings(database_url=unreachable_database_url, environment="test")  # pyright: ignore[reportArgumentType]
    engine = create_engine(settings)
    try:
        await verify_database_role(engine)  # no error: nothing to check yet
    finally:
        await engine.dispose()


async def test_the_web_app_does_not_start_with_a_privileged_role(database_url: str) -> None:
    settings = Settings(database_url=database_url, environment="test")  # pyright: ignore[reportArgumentType]
    app = create_app(settings)
    with pytest.raises(DatabaseRoleError):
        async with app.router.lifespan_context(app):
            pytest.fail("the app must not start")


async def test_the_web_app_starts_with_the_app_role(app_database_url: str) -> None:
    settings = Settings(
        database_url=app_database_url,  # pyright: ignore[reportArgumentType]
        environment="test",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/ready")).status_code == 200


def test_every_problem_has_a_flag_and_the_messages_are_distinct() -> None:
    good = [f"0:{k}={v}" for k, v in EXPECTED_ROLE_SETTINGS.items()]
    assert len(set(PROBLEMS)) == len(PROBLEMS) == 7
    assert problems_for((*(False,) * 7, good)) == []
    assert problems_for((True, False, False, False, True, False, True, good)) == [
        PROBLEMS[0],
        PROBLEMS[4],
        PROBLEMS[6],
    ]


@pytest.mark.parametrize("row", [None, (), (False,) * 7, (False,) * 9, ([],)])
def test_a_malformed_role_row_raises(row: object) -> None:
    with pytest.raises(ValueError, match="seven flags"):
        problems_for(row)


async def test_a_refused_login_is_fatal_with_a_clear_message_and_no_password(
    database_url: str,
) -> None:
    wrong = "wrong-" + secrets.token_hex(6)
    settings = Settings(
        database_url=url_for(database_url, "ownvitals_app", wrong),  # pyright: ignore[reportArgumentType]
        environment="test",
    )
    engine = create_engine(settings)
    try:
        with pytest.raises(DatabaseRoleError, match="refused the configured credentials") as err:
            await verify_database_role(engine)
        assert wrong not in str(err.value)
    finally:
        await engine.dispose()
    app = create_app(settings)
    with pytest.raises(DatabaseRoleError, match="refused the configured credentials"):
        async with app.router.lifespan_context(app):
            pytest.fail("the app must not start")


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def test_a_database_that_comes_up_later_is_still_checked_on_first_connection(
    database_url: str,
) -> None:
    from sqlalchemy.engine import make_url

    real = make_url(database_url)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # A superuser URL, but nothing listens on the port yet: startup cannot check it.
    settings = Settings(
        database_url=real.set(host="127.0.0.1", port=port).render_as_string(hide_password=False),  # pyright: ignore[reportArgumentType]
        environment="test",
    )
    engine = create_engine(settings)

    async def forward(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        server_r, server_w = await asyncio.open_connection(real.host, real.port)
        await asyncio.gather(_pipe(client_r, server_w), _pipe(server_r, client_w))

    try:
        await verify_database_role(engine)  # no error: the database is down
        server = await asyncio.start_server(forward, "127.0.0.1", port)  # now it "comes up"
        try:
            with pytest.raises(DatabaseRoleError, match="is a superuser"):
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
        finally:
            # The pool lets go of any connection first: wait_closed() waits for every forwarded
            # connection, so a connection the check failed to refuse would otherwise hang here.
            await engine.dispose()
            server.close()
            async with asyncio.timeout(10):
                await server.wait_closed()
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("change", "undo"),
    [
        ("SET statement_timeout = 0", "SET statement_timeout = '30s'"),
        ("SET session_replication_role = replica", "RESET session_replication_role"),
        ("RESET lock_timeout", "SET lock_timeout = '5s'"),
    ],
)
async def test_a_role_that_changed_its_own_settings_is_refused(
    database_url: str, probe_roles: None, change: str, undo: str
) -> None:
    """ALTER ROLE ... SET by the role itself persists and Postgres cannot forbid it."""
    admin = create_sync_engine(database_url, isolation_level="AUTOCOMMIT")
    settings = settings_for(database_url, "guard_plain")
    engine = create_engine(settings)
    try:
        with admin.connect() as connection:
            connection.execute(text(f"ALTER ROLE guard_plain {change}"))
        with pytest.raises(DatabaseRoleError, match="role settings other than"):
            await verify_database_role(engine)
        with pytest.raises(DatabaseRoleError, match="role settings other than"):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        with admin.connect() as connection:
            connection.execute(text(f"ALTER ROLE guard_plain {undo}"))
        await verify_database_role(engine)
    finally:
        await engine.dispose()
        admin.dispose()


def test_settings_drift_is_a_problem_of_its_own() -> None:
    good = [f"0:{k}={v}" for k, v in EXPECTED_ROLE_SETTINGS.items()]
    flags = (False,) * 7
    assert problems_for((*flags, good)) == []
    assert problems_for((*flags, good[:2])) == [SETTINGS_PROBLEM]
    assert problems_for((*flags, [*good, "0:search_path=x"])) == [SETTINGS_PROBLEM]
    assert problems_for((*flags, ["1:" + g[2:] for g in good])) == [SETTINGS_PROBLEM]
