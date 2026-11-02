"""Partitions and the webhook-secret function (#21). Synthetic ids and filler bytes only.

A refused call has to roll the transaction back, so ``pytest.raises`` stays around the
connection rather than inside it.
"""
# ruff: noqa: SIM117, E501, PT012

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from jobtools import engine_for, ensure_user, install_queue, uninstall_queue
from ownvitals.db import SYSTEM_TABLES
from ownvitals.db.queue_check import QUEUE_QUERY, queue_problems
from ownvitals.db.queue_locks import PARTITIONS, WEBHOOK_SECRET_ROTATION
from queue_guard import check_queue
from rls_guard import check_tables
from withingsschema import (
    HMAC,
    PEPPER,
    PEPPER_2,
    SECRET,
    SEED,
    SEED_2,
    install,
    new_app,
    seed_keys,
    uninstall,
)


@pytest.fixture
def functions(schema: None, database_url: str) -> Iterator[None]:
    install(database_url)
    seed_keys(database_url)
    try:
        yield
    finally:
        uninstall(database_url)


def test_partitions_are_created_lazily_for_the_months_asked(
    functions: None, database_url: str, app_database_url: str
) -> None:
    with engine_for(app_database_url) as engine, engine.connect() as conn:
        trans = conn.begin()
        created = conn.execute(
            text(
                "SELECT ensure_month_partitions('sleep_series', DATE '2026-11-01', "
                "DATE '2026-11-01')"
            )
        ).scalar_one()
        again = conn.execute(
            text("SELECT ensure_month_partitions('intraday', DATE '2026-11-01', DATE '2026-12-01')")
        ).scalar_one()
        locks = conn.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid() AND classid = :ns"
            ),
            {"ns": PARTITIONS},
        ).scalar_one()
        names = (
            conn.execute(
                text(
                    "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relispartition AND c.relkind = 'r' ORDER BY 1"
                )
            )
            .scalars()
            .all()
        )
        assert check_tables(conn, SYSTEM_TABLES) == []
        trans.rollback()
    assert created == 1
    assert again == 2
    assert locks == 2
    assert names == ["intraday_y2026m11", "intraday_y2026m12", "sleep_series_y2026m11"]
    with engine_for(database_url) as engine, engine.connect() as conn:
        left = conn.execute(
            text("SELECT count(*) FROM pg_class c WHERE c.relispartition")
        ).scalar_one()
    assert left == 0


def test_a_month_bound_is_the_utc_month_in_another_session_timezone(
    functions: None, app_database_url: str
) -> None:
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        conn.execute(text("SELECT set_config('TimeZone', 'America/New_York', true)"))
        zone = conn.execute(text("SHOW TimeZone")).scalar_one()
        conn.execute(
            text(
                "SELECT ensure_month_partitions('sleep_series', DATE '2026-11-01', "
                "DATE '2026-11-01')"
            )
        )
        # pg_get_expr renders timestamptz literals in the session TimeZone. Read them in UTC
        # so the assertion is the month itself: UTC midnight, including where New York's
        # offset changes between November and December.
        conn.execute(text("SELECT set_config('TimeZone', 'UTC', true)"))
        bound = conn.execute(
            text(
                "SELECT pg_get_expr(c.relpartbound, c.oid) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'sleep_series_y2026m11'"
            )
        ).scalar_one()
        config = conn.execute(
            text(
                "SELECT proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.proname = 'ensure_month_partitions'"
            )
        ).scalar_one()
    assert zone == "America/New_York"
    assert bound == "FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00')"
    assert list(config) == ["search_path=pg_catalog, pg_temp", "TimeZone=UTC"]


def test_the_queue_script_installs_again_while_the_withings_functions_exist(
    functions: None, database_url: str
) -> None:
    install_queue(database_url)
    try:
        install_queue(database_url)
        with engine_for(database_url) as engine, engine.connect() as conn:
            row = conn.execute(text(QUEUE_QUERY)).one()
            assert queue_problems(tuple(row)) == []
            assert check_queue(conn) == []
    finally:
        uninstall_queue(database_url)


def test_a_month_outside_the_floor_or_the_ceiling_is_refused(
    functions: None, app_database_url: str
) -> None:
    refused = (
        (
            "SELECT ensure_month_partitions('sleep_series', DATE '2008-12-01', DATE '2008-12-01')",
            "month_out_of_range",
        ),
        (
            "SELECT ensure_month_partitions('sleep_series', "
            "(date_trunc('month', now() + interval '4 months'))::date, "
            "(date_trunc('month', now() + interval '4 months'))::date)",
            "month_out_of_range",
        ),
        (
            "SELECT ensure_month_partitions('measures', DATE '2026-11-01', DATE '2026-11-01')",
            "table_not_allowed",
        ),
    )
    for sql, match in refused:
        with pytest.raises(DBAPIError, match=match), engine_for(app_database_url) as engine:
            with engine.begin() as conn:
                conn.execute(text(sql))


def test_the_app_role_reaches_a_partition_only_through_its_parent(
    functions: None, database_url: str, app_database_url: str
) -> None:
    owner = uuid.uuid4()
    with engine_for(database_url) as engine, engine.begin() as conn:
        ensure_user(conn, owner)
        connection = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO connections (id, owner_id, provider, status) "
                "VALUES (:id, :owner, 'withings', 'disconnected')"
            ),
            {"id": connection, "owner": owner},
        )
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "SELECT ensure_month_partitions('sleep_series', DATE '2026-11-01', DATE '2026-11-01')"
            )
        )
        denied = conn.execute(
            text(
                "SELECT has_table_privilege('ownvitals_app', "
                "'public.sleep_series_y2026m11', 'SELECT')"
            )
        ).scalar_one()
        conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(owner)})
        conn.execute(
            text(
                "INSERT INTO sleep_series (owner_id, ts, source_tz, connection_id, series, "
                "value_raw, unit) VALUES (:o, '2026-11-02 00:00+00', 'UTC', :c, 'hr', 1, 0)"
            ),
            {"o": owner, "c": connection},
        )
        with pytest.raises(DBAPIError, match="permission denied"):
            conn.execute(
                text("INSERT INTO sleep_series_y2026m11 (owner_id) VALUES (:o)"), {"o": owner}
            )
    assert denied is False


def _apply(conn: object, app_id: int, operation: str, **params: object) -> str:
    values = {
        "app": app_id,
        "op": operation,
        "secret": params.get("secret"),
        "seed": params.get("seed"),
        "pepper": params.get("pepper"),
        "hmac": params.get("hmac", HMAC),
        "active": params.get("active", datetime(2026, 11, 2, tzinfo=UTC)),
        "seen_at": params.get("seen_at"),
        "seen_reason": params.get("seen_reason"),
    }
    return str(
        conn.execute(  # type: ignore[attr-defined]
            text(
                "SELECT webhook_secrets_apply(:app, :op, :secret, :seed, :pepper, :hmac, "
                ":active, :seen_at, :seen_reason)"
            ),
            values,
        ).scalar_one()
    )


def test_append_retire_and_the_audit_row_hold_no_hmac(
    functions: None, database_url: str, app_database_url: str
) -> None:
    app_id = new_app(database_url)
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        assert _apply(conn, app_id, "append", secret=SECRET, seed=SEED, pepper=PEPPER) == "append"
        stored = conn.execute(
            text("SELECT webhook_secrets->0->>'hmac' FROM withings_apps WHERE id = :id"),
            {"id": app_id},
        ).scalar_one()
        assert stored == HMAC.hex()
        locks = conn.execute(
            text(
                "SELECT classid, objid FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid() AND classid = :ns"
            ),
            {"ns": WEBHOOK_SECRET_ROTATION},
        ).all()
        assert locks == [(WEBHOOK_SECRET_ROTATION, app_id)]
        assert _apply(conn, app_id, "retire", secret=SECRET) == "retire"
        assert _apply(conn, app_id, "delete-retired", secret=SECRET) == "delete-retired"
    with engine_for(database_url) as engine, engine.connect() as conn:
        rows = (
            conn.execute(
                text("SELECT params::text FROM admin_audit WHERE action = 'webhook_secrets.apply'")
            )
            .scalars()
            .all()
        )
    assert rows
    assert all(HMAC.hex() not in params and "hmac" not in params for params in rows)


def test_a_compromise_append_refuses_an_unretired_entry_or_a_reused_seed(
    functions: None, database_url: str, app_database_url: str
) -> None:
    app_id = new_app(database_url)
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        _apply(conn, app_id, "append", secret=SECRET, seed=SEED, pepper=PEPPER)
    seen = datetime(2026, 11, 1, tzinfo=UTC)
    with engine_for(database_url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE withings_apps SET rotation_requested_at = :seen, "
                "rotation_reason = 'compromise' WHERE id = :id"
            ),
            {"id": app_id, "seen": seen},
        )
    with pytest.raises(DBAPIError, match="unretired_entries"):
        with engine_for(app_database_url) as engine, engine.begin() as conn:
            _apply(
                conn,
                app_id,
                "append",
                secret=str(uuid.uuid4()),
                seed=SEED_2,
                pepper=PEPPER,
                seen_at=seen,
                seen_reason="compromise",
            )
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        _apply(conn, app_id, "retire-all", seen_at=seen, seen_reason="compromise")
    with pytest.raises(DBAPIError, match="seed_reused"):
        with engine_for(app_database_url) as engine, engine.begin() as conn:
            _apply(
                conn,
                app_id,
                "append",
                secret=str(uuid.uuid4()),
                seed=SEED,
                pepper=PEPPER_2,
                seen_at=seen,
                seen_reason="compromise",
            )
    fresh = str(uuid.uuid4())
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        assert (
            _apply(
                conn,
                app_id,
                "append",
                secret=fresh,
                seed=SEED_2,
                pepper=PEPPER_2,
                seen_at=seen,
                seen_reason="compromise",
            )
            == "append"
        )
        reason = conn.execute(
            text("SELECT rotation_reason FROM withings_apps WHERE id = :id"), {"id": app_id}
        ).scalar_one()
    assert reason is None


def test_compare_and_clear_refuses_a_request_that_moved(
    functions: None, database_url: str, app_database_url: str
) -> None:
    app_id = new_app(database_url)
    with engine_for(database_url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE withings_apps SET rotation_requested_at = '2026-11-01 00:00+00', "
                "rotation_reason = 'routine' WHERE id = :id"
            ),
            {"id": app_id},
        )
        conn.execute(
            text("UPDATE withings_apps SET rotation_reason = 'compromise' WHERE id = :id"),
            {"id": app_id},
        )
    with pytest.raises(DBAPIError, match="compromise_pending"):
        with engine_for(app_database_url) as engine, engine.begin() as conn:
            _apply(
                conn,
                app_id,
                "retire-all",
                seen_at=datetime(2026, 11, 1, tzinfo=UTC),
                seen_reason="routine",
            )
    with engine_for(database_url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE withings_apps SET rotation_requested_at = '2026-11-03 00:00+00' "
                "WHERE id = :id"
            ),
            {"id": app_id},
        )
    with pytest.raises(DBAPIError, match="stale_request"):
        with engine_for(app_database_url) as engine, engine.begin() as conn:
            _apply(
                conn,
                app_id,
                "append",
                secret=str(uuid.uuid4()),
                seed=SEED_2,
                pepper=PEPPER,
                seen_at=datetime(2026, 11, 1, tzinfo=UTC),
                seen_reason="compromise",
            )
    with engine_for(database_url) as engine, engine.connect() as conn:
        still = conn.execute(
            text("SELECT rotation_requested_at FROM withings_apps WHERE id = :id"), {"id": app_id}
        ).scalar_one()
    assert still is not None


def test_the_webhook_secret_role_has_no_bypass_and_no_client_secret(
    functions: None, database_url: str
) -> None:
    with engine_for(database_url) as engine, engine.connect() as conn:
        role = conn.execute(
            text(
                "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
                "rolreplication, rolinherit FROM pg_roles WHERE rolname = 'ownvitals_webhook_secrets'"
            )
        ).one()
        assert tuple(role) == (False, False, False, False, False, False, False)
        assert not conn.execute(
            text(
                "SELECT has_column_privilege('ownvitals_webhook_secrets', "
                "'public.withings_apps', 'client_secret', 'SELECT')"
            )
        ).scalar_one()
        assert not conn.execute(
            text(
                "SELECT has_column_privilege('ownvitals_webhook_secrets', "
                "'public.connection_keks', 'check_value', 'SELECT')"
            )
        ).scalar_one()
        assert conn.execute(
            text(
                "SELECT has_column_privilege('ownvitals_webhook_secrets', "
                "'public.connection_keks', 'key_id', 'SELECT')"
            )
        ).scalar_one()
        assert not conn.execute(
            text("SELECT has_schema_privilege('ownvitals_webhook_secrets', 'public', 'CREATE')")
        ).scalar_one()
    with engine_for(database_url) as engine, engine.connect() as conn:
        with pytest.raises(DBAPIError, match="bad_isolation"):
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
            conn.execute(
                text(
                    "SELECT webhook_secrets_apply(1, 'retire-all', NULL, NULL, NULL, "
                    "NULL, NULL, NULL, NULL)"
                )
            )
