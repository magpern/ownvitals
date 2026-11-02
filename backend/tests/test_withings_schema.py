"""The raw Withings schema (#21): guard 1, owner-inclusive foreign keys, RLS, and the
webhook_events definer. Synthetic rows only; no real account or measurement.
"""

# Nested transactions stay inside pytest.raises so a refusal rolls back.

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from dbtools import alembic_config, public_tables
from jobtools import engine_for, ensure_user
from ownvitals.db import SYSTEM_TABLES
from ownvitals.withings.tables import APP_WITHINGS_COLUMNS, CONNECTIONS_WITHINGS_USER_INDEX
from rls_guard import check_tables
from withingsschema import HASH, install, new_app, uninstall

REPO = Path(__file__).resolve().parents[2]
ROLES = REPO / "docker" / "postgres" / "init" / "10-roles.sql"
SRC = REPO / "backend" / "src" / "ownvitals"
# The scheduler's maintenance step and every writer of a partitioned table are the callers.
# None of those modules exist yet; each joins this set in the PR that calls the function.
# db/queue_locks.py only records advisory-lock namespace 3.
PARTITION_CALLERS = frozenset({"db/queue_locks.py"})


def test_only_listed_modules_name_the_partition_function() -> None:
    needle = "ensure_month_partitions"
    found = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if needle in path.read_text(encoding="utf-8")
    )
    assert found == sorted(PARTITION_CALLERS)


def test_the_roles_script_restores_the_withings_grants() -> None:
    script = ROLES.read_text(encoding="utf-8")
    assert (
        f"GRANT SELECT ({', '.join(APP_WITHINGS_COLUMNS)}) ON TABLE public.withings_apps" in script
    )
    assert "REVOKE ALL ON TABLE public.webhook_events FROM ownvitals_app" in script
    assert "REVOKE ALL ON SEQUENCE public.withings_apps_id_seq FROM ownvitals_app" in script


def test_the_migrated_schema_meets_guard_1(schema: None, database_url: str) -> None:
    with engine_for(database_url) as engine, engine.connect() as conn:
        assert check_tables(conn, SYSTEM_TABLES) == []
        index = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
            {"n": CONNECTIONS_WITHINGS_USER_INDEX},
        ).scalar_one()
    assert "app_id" in index
    assert "withings_userid" in index


def test_upgrade_and_downgrade_round_trip(
    schema: None, database_url: str, owner_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OWNVITALS_MIGRATIONS_DATABASE_URL", owner_database_url)
    config = alembic_config()
    command.downgrade(config, "0008_withings_jobs")
    names = public_tables(owner_database_url)
    assert "withings_apps" not in names
    assert "sleep_series" not in names
    command.upgrade(config, "0009_withings_schema")
    names = public_tables(owner_database_url)
    assert {"withings_apps", "webhook_events", "sleep_series", "intraday", "measures"} <= names
    with engine_for(database_url) as engine, engine.connect() as conn:
        assert check_tables(conn, SYSTEM_TABLES) == []


def _connection(conn: object, owner: uuid.UUID) -> uuid.UUID:
    ensure_user(conn, owner)  # type: ignore[arg-type]
    connection = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO connections (id, owner_id, provider, status) "
            "VALUES (:id, :owner, 'withings', 'disconnected')"
        ),
        {"id": connection, "owner": owner},
    )
    return connection


def test_a_child_cannot_reference_another_owners_connection(
    schema: None, database_url: str
) -> None:
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    with engine_for(database_url) as engine, engine.begin() as conn:
        connection = _connection(conn, owner_a)
        _connection(conn, owner_b)
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO measure_groups (owner_id, connection_id, grpid, measured_at, "
                    "source_tz, payload_hash) VALUES (:owner, :connection, 1, now(), 'UTC', :hash)"
                ),
                {"owner": owner_b, "connection": connection, "hash": HASH},
            )


def test_one_owner_cannot_read_anothers_device(
    schema: None, database_url: str, app_database_url: str
) -> None:
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    with engine_for(database_url) as engine, engine.begin() as conn:
        connection = _connection(conn, owner_a)
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(owner_a)})
        conn.execute(
            text(
                "INSERT INTO devices (owner_id, connection_id, hash_deviceid) "
                "VALUES (:owner, :connection, 'synthetic-device')"
            ),
            {"owner": owner_a, "connection": connection},
        )
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(owner_b)})
        seen = conn.execute(text("SELECT hash_deviceid FROM devices")).all()
    assert seen == []


def test_linking_the_same_withings_account_twice_is_refused(
    schema: None, database_url: str
) -> None:
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    public = '{"app_id": 7, "withings_userid": 11, "scopes": ["user.metrics"]}'
    with engine_for(database_url) as engine, engine.begin() as conn:
        _connection(conn, owner_a)
        _connection(conn, owner_b)
        conn.execute(
            text("UPDATE connections SET public = CAST(:p AS jsonb) WHERE owner_id = :o"),
            {"p": public, "o": owner_a},
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                text("UPDATE connections SET public = CAST(:p AS jsonb) WHERE owner_id = :o"),
                {"p": public, "o": owner_b},
            )


@pytest.fixture
def functions(schema: None, database_url: str) -> Iterator[None]:
    install(database_url)
    try:
        yield
    finally:
        uninstall(database_url)


def test_webhook_events_are_writable_only_through_the_definer(
    functions: None, database_url: str, app_database_url: str
) -> None:
    app_id = new_app(database_url)
    with engine_for(app_database_url) as engine, engine.begin() as conn:  # noqa: SIM117
        with pytest.raises(DBAPIError, match="permission denied"):
            conn.execute(
                text(
                    "INSERT INTO webhook_events (app_id, withings_userid, appli, startdate, "
                    "enddate, dedupe_key) VALUES (:a, 1, 1, 1, 2, 'synthetic-direct')"
                ),
                {"a": app_id},
            )
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        inserted = conn.execute(
            text("SELECT webhook_event_insert(:a, 42, 1, 10, 20, 'synthetic-event', NULL)"),
            {"a": app_id},
        ).scalar_one()
    assert inserted is not None
    with engine_for(app_database_url) as engine, engine.begin() as conn:  # noqa: SIM117
        with pytest.raises(DBAPIError, match="permission denied"):
            conn.execute(text("SELECT count(*) FROM webhook_events"))
