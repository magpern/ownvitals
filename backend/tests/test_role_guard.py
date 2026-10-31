"""Guard 2 of ADR 0002 section 7: the roles and privileges of section 5, checked as an allowlist
on the real database, plus proof that the check can fail."""

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from dbtools import CHILD, PARENT, migrated, owner_connection
from role_guard import check_roles


@pytest.fixture
def schema(owner_database_url: str) -> Iterator[None]:
    """The migrated schema (all revisions), as ``migrate`` leaves it."""
    with migrated(owner_database_url):
        yield


def exec_all(connection: Connection, *statements: str) -> None:
    for statement in statements:
        connection.execute(text(statement))


@pytest.mark.usefixtures("schema")
def test_roles_and_privileges_match_the_adr(database_url: str, owned_tables: None) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert check_roles(connection) == []
    finally:
        engine.dispose()


def admin_problems(database_url: str, *statements: str, undo: tuple[str, ...] = ()) -> list[str]:
    engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            try:
                exec_all(connection, *statements)
                return check_roles(connection)
            finally:
                exec_all(connection, *undo)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("statements", "undo", "expected"),
    [
        (
            [f"GRANT TRUNCATE ON {PARENT} TO ownvitals_app"],
            [f"REVOKE TRUNCATE ON {PARENT} FROM ownvitals_app"],
            "privileges must be exactly",
        ),
        (
            [f"GRANT REFERENCES, TRIGGER, MAINTAIN ON {PARENT} TO ownvitals_app"],
            [f"REVOKE REFERENCES, TRIGGER, MAINTAIN ON {PARENT} FROM ownvitals_app"],
            "privileges must be exactly",
        ),
        (
            [f"GRANT SELECT ON {CHILD} TO PUBLIC"],
            [f"REVOKE SELECT ON {CHILD} FROM PUBLIC"],  # noqa: S608
            "PUBLIC must have no privilege",
        ),
        (
            [f"GRANT SELECT (name) ON {PARENT} TO PUBLIC"],
            [f"REVOKE SELECT (name) ON {PARENT} FROM PUBLIC"],  # noqa: S608
            "column-level",
        ),
        (
            ["GRANT TEMP ON DATABASE {db} TO PUBLIC"],
            ["REVOKE TEMP ON DATABASE {db} FROM PUBLIC"],
            "PUBLIC must have no privilege on the database",
        ),
        (
            ["GRANT CREATE ON SCHEMA public TO ownvitals_app"],
            ["REVOKE CREATE ON SCHEMA public FROM ownvitals_app"],
            "USAGE only on public",
        ),
        (
            ["GRANT USAGE ON SCHEMA public TO PUBLIC"],
            ["REVOKE USAGE ON SCHEMA public FROM PUBLIC"],
            "PUBLIC must have no privilege on public",
        ),
        (
            ["ALTER ROLE ownvitals_app BYPASSRLS"],
            ["ALTER ROLE ownvitals_app NOBYPASSRLS"],
            "must not have rolbypassrls",
        ),
        (
            ["ALTER ROLE ownvitals_owner BYPASSRLS"],
            ["ALTER ROLE ownvitals_owner NOBYPASSRLS"],
            "must not have rolbypassrls",
        ),
        (
            ["CREATE ROLE guard_bypass NOLOGIN BYPASSRLS", "GRANT guard_bypass TO ownvitals_owner"],
            ["DROP ROLE guard_bypass"],
            "must be a member of no role",
        ),
        (
            ["ALTER ROLE ownvitals_app SET lock_timeout = '1h'"],
            ["ALTER ROLE ownvitals_app SET lock_timeout = '5s'"],
            "role settings must be exactly",
        ),
        (
            ["ALTER ROLE ownvitals_app IN DATABASE {db} SET statement_timeout = 0"],
            ["ALTER ROLE ownvitals_app IN DATABASE {db} RESET ALL"],
            "none per database",
        ),
        (
            ["ALTER ROLE ownvitals_app SET session_replication_role = replica"],
            ["ALTER ROLE ownvitals_app RESET session_replication_role"],
            "role settings must be exactly",
        ),
        (
            ["ALTER ROLE ownvitals_app SET search_path = pg_temp"],
            ["ALTER ROLE ownvitals_app RESET search_path"],
            "role settings must be exactly",
        ),
        (
            ["ALTER DATABASE {db} SET app.user_id = '00000000-0000-4000-8000-000000000001'"],
            ["ALTER DATABASE {db} RESET app.user_id"],
            "default for an app.* setting",
        ),
        (
            ["ALTER ROLE ALL SET app.user_id = '00000000-0000-4000-8000-000000000001'"],
            ["ALTER ROLE ALL RESET app.user_id"],
            "default for an app.* setting",
        ),
        (
            ["ALTER ROLE ALL IN DATABASE {db} SET \"App.Note\" = 'x'"],
            ['ALTER ROLE ALL IN DATABASE {db} RESET "App.Note"'],
            "default for an app.* setting",
        ),
        (
            ["ALTER ROLE ownvitals_owner SET app.user_id = '00000000-0000-4000-8000-000000000001'"],
            ["ALTER ROLE ownvitals_owner RESET app.user_id"],
            "default for an app.* setting",
        ),
        (
            ["ALTER ROLE ownvitals_app REPLICATION"],
            ["ALTER ROLE ownvitals_app NOREPLICATION"],
            "must not have rolreplication",
        ),
        (
            ["GRANT ownvitals_owner TO ownvitals_app WITH INHERIT FALSE, SET FALSE"],
            ["REVOKE ownvitals_owner FROM ownvitals_app"],
            "must be a member of no role",
        ),
        (
            ["GRANT CONNECT ON DATABASE postgres TO PUBLIC"],
            ["REVOKE CONNECT ON DATABASE postgres FROM PUBLIC"],
            "must not connect to",
        ),
        (
            [
                "ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner "
                "GRANT MAINTAIN ON TABLES TO ownvitals_app"
            ],
            [
                "ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner "
                "REVOKE MAINTAIN ON TABLES FROM ownvitals_app"
            ],
            "unexpected default privileges",
        ),
        (
            [
                "ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner IN SCHEMA public "
                "GRANT TRUNCATE ON TABLES TO ownvitals_app"
            ],
            [
                "ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner IN SCHEMA public "
                "REVOKE TRUNCATE ON TABLES FROM ownvitals_app"
            ],
            "default privileges on r in public",
        ),
        (
            [
                "ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner "
                "GRANT EXECUTE ON FUNCTIONS TO PUBLIC"
            ],
            [
                "ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner "
                "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
            ],
            "default privileges for functions must be set",
        ),
    ],
)
def test_the_role_guard_fails_when_a_privilege_drifts(
    database_url: str,
    owned_tables: None,
    statements: list[str],
    undo: list[str],
    expected: str,
) -> None:
    database = create_engine(database_url)
    with database.connect() as connection:
        db = connection.execute(text("SELECT current_database()")).scalar_one()
    database.dispose()
    found = admin_problems(
        database_url,
        *[s.replace("{db}", db) for s in statements],
        undo=tuple(u.replace("{db}", db) for u in undo),
    )
    assert any(expected in problem for problem in found), found
    assert admin_problems(database_url) == [], "the undo statements must restore the state"


def test_a_function_made_by_the_owner_is_not_callable_by_public_or_the_app(
    database_url: str, owner_database_url: str, app_database_url: str
) -> None:
    with owner_connection(owner_database_url) as connection:
        exec_all(connection, "CREATE FUNCTION t_fn() RETURNS int LANGUAGE sql AS 'SELECT 1'")
    try:
        assert admin_problems(database_url) == []
        engine = create_engine(app_database_url)
        try:
            with (
                engine.connect() as connection,
                pytest.raises(Exception, match="permission denied"),
            ):
                connection.execute(text("SELECT public.t_fn()"))
        finally:
            engine.dispose()
    finally:
        with owner_connection(owner_database_url) as connection:
            exec_all(connection, "DROP FUNCTION t_fn()")


def test_public_cannot_connect_to_the_database(database_url: str) -> None:
    engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    probe = engine.url.set(username="guard_probe", password="probe-password")
    try:
        with engine.connect() as connection:
            exec_all(connection, "CREATE ROLE guard_probe LOGIN PASSWORD 'probe-password'")
        try:
            with pytest.raises(OperationalError, match="permission denied for database"):
                create_engine(probe).connect()
        finally:
            with engine.connect() as connection:
                exec_all(connection, "DROP ROLE guard_probe")
    finally:
        engine.dispose()


@pytest.mark.usefixtures("schema")
def test_the_role_guard_fails_when_migration_bookkeeping_is_reopened(database_url: str) -> None:
    """Migration 0002 closes ``alembic_version`` to the app role; reopening it fails guard 2."""
    assert admin_problems(database_url) == []
    found = admin_problems(
        database_url,
        "GRANT SELECT, UPDATE ON alembic_version TO ownvitals_app",
        undo=("REVOKE ALL ON alembic_version FROM ownvitals_app",),
    )
    assert any(p.startswith("alembic_version:r: ownvitals_app privileges") for p in found), found


def test_the_role_guard_fails_for_another_grantee(database_url: str, owned_tables: None) -> None:
    found = admin_problems(
        database_url,
        "CREATE ROLE guard_other NOLOGIN",
        f"GRANT SELECT ON {PARENT} TO guard_other",
        undo=(f"REVOKE ALL ON {PARENT} FROM guard_other", "DROP ROLE guard_other"),
    )
    assert f"{PARENT}:r: unexpected grantee" in found, found


def test_the_role_guard_fails_when_the_app_may_execute_a_function(
    database_url: str, owner_database_url: str
) -> None:
    with owner_connection(owner_database_url) as connection:
        exec_all(connection, "CREATE FUNCTION t_fn() RETURNS int LANGUAGE sql AS 'SELECT 1'")
    try:
        found = admin_problems(
            database_url,
            "GRANT EXECUTE ON FUNCTION t_fn() TO ownvitals_app",
            undo=("REVOKE EXECUTE ON FUNCTION t_fn() FROM ownvitals_app",),
        )
        assert any("ownvitals_app may execute no function" in p for p in found), found
    finally:
        with owner_connection(owner_database_url) as connection:
            exec_all(connection, "DROP FUNCTION t_fn()")
