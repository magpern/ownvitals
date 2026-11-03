"""The derived schema (#181): guard 1 on the new tables, the episode index, the system tier,
the check constraints, the vocabularies and the downgrade. Synthetic rows only; no real
account or measurement.
"""

# Each refusal runs in a nested transaction inside pytest.raises so it rolls back alone.

import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from dbtools import alembic_config, public_tables
from jobtools import engine_for, ensure_user
from ownvitals.db import SYSTEM_TABLES
from ownvitals.derived import tables as derived
from ownvitals.derived.tables import ACTIVE_EPISODE_INDEX, DERIVED_OWNED
from ownvitals.ingest.partitions import PARTITIONED
from rls_guard import check_tables

MIGRATION = (
    Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0011_analysis_schema.py"
)
HASH = b"\x33" * 32  # a labelled synthetic fingerprint, not a digest of anything
pytestmark = pytest.mark.usefixtures("schema")


@pytest.fixture
def conn(database_url: str) -> Iterator[Connection]:
    """A superuser transaction with one synthetic user; rolled back afterwards."""
    with engine_for(database_url) as engine, engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _user(conn: Connection) -> uuid.UUID:
    owner = uuid.uuid4()
    ensure_user(conn, owner)
    return owner


def _finding(conn: Connection, owner: uuid.UUID, **overrides: Any) -> uuid.UUID:
    row: dict[str, Any] = {
        "rule_id": "C1",
        "rule_version": "1",
        "tier": "trend",
        "severity": "attention",
        "confidence": "medium",
        "origin": "live",
        "subject": "resting_hr",
        "status": "active",
        "ended_at": None,
        "notify_state": "none",
        "notified_at": None,
        **overrides,
    }
    return conn.execute(
        text(
            "INSERT INTO findings (owner_id, rule_id, rule_version, tier, severity, confidence, "
            "origin, subject, window_start, window_end, input_revision, input_fingerprint, "
            "status, ended_at, notify_state, notified_at) VALUES (:owner, :rule_id, "
            ":rule_version, :tier, :severity, :confidence, :origin, :subject, '2026-10-01', "
            "'2026-10-28', 0, :hash, :status, :ended_at, :notify_state, :notified_at) "
            "RETURNING id"
        ),
        {"owner": owner, "hash": HASH, **row},
    ).scalar_one()


def _refused(conn: Connection, call: Any) -> None:
    with pytest.raises(IntegrityError), conn.begin_nested():
        call()


def test_the_migrated_schema_meets_guard_1(database_url: str) -> None:
    with engine_for(database_url) as engine, engine.connect() as connection:
        assert check_tables(connection, SYSTEM_TABLES) == []
        kinds = dict(
            connection.execute(
                text(
                    "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
                    "AND c.relname = ANY(:names)"
                ),
                {"names": list(DERIVED_OWNED)},
            ).all()
        )
    assert kinds == {name: ("p" if name == "observations" else "r") for name in DERIVED_OWNED}


def test_observations_get_partitions_from_the_definer(
    app_database_url: str, database_url: str
) -> None:
    assert "observations" in PARTITIONED
    with engine_for(app_database_url) as engine, engine.begin() as connection:
        created = connection.execute(
            text(
                "SELECT ensure_month_partitions('observations', DATE '2026-10-01', "
                "DATE '2026-11-01')"
            )
        ).scalar_one()
    assert created == 2
    try:
        with engine_for(database_url) as engine, engine.connect() as connection:
            # Guard 1 checks rules 1 to 5 on every partition: forced RLS, the policy, no app
            # privilege, no foreign key into it.
            assert check_tables(connection, SYSTEM_TABLES) == []
    finally:
        with engine_for(database_url) as engine, engine.begin() as connection:
            connection.execute(text("DROP TABLE public.observations_y2026m10"))
            connection.execute(text("DROP TABLE public.observations_y2026m11"))


def test_two_active_episodes_of_one_subject_are_refused(conn: Connection) -> None:
    owner, other = _user(conn), _user(conn)
    _finding(conn, owner)
    _refused(conn, lambda: _finding(conn, owner))
    # Another subject, another rule or another owner is a separate episode.
    _finding(conn, owner, subject="weight")
    _finding(conn, owner, rule_id="N2")
    _finding(conn, other)


def test_an_ended_episode_and_a_new_active_one_coexist(conn: Connection) -> None:
    owner = _user(conn)
    first = _finding(conn, owner)
    conn.execute(
        text("UPDATE findings SET status = 'ended', ended_at = now() WHERE id = :id"),
        {"id": first},
    )
    _finding(conn, owner)
    _finding(conn, owner, status="ended", ended_at="2026-10-30T00:00:00Z")
    states = conn.execute(
        text("SELECT status, count(*) FROM findings WHERE owner_id = :o GROUP BY 1 ORDER BY 1"),
        {"o": owner},
    ).all()
    assert [tuple(row) for row in states] == [("active", 1), ("ended", 2)]


def test_the_active_episode_index_is_partial_and_owner_scoped(conn: Connection) -> None:
    definition = conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
        {"n": ACTIVE_EPISODE_INDEX},
    ).scalar_one()
    assert "UNIQUE" in definition
    assert "(owner_id, rule_id, subject)" in definition
    assert "WHERE (status = 'active'::text)" in definition


def test_the_system_tier_is_q1_only(conn: Connection) -> None:
    owner = _user(conn)
    _finding(conn, owner, rule_id="Q1", tier="system", severity="info", confidence="high")
    _refused(
        conn,
        lambda: _finding(conn, owner, tier="system", confidence="high", subject="other"),
    )
    # Q1 is always system (Amendment 3), never one of the other tiers.
    _refused(conn, lambda: _finding(conn, owner, rule_id="Q1", subject="battery"))
    _refused(conn, lambda: _finding(conn, owner, tier="body"))


def test_finding_states_stay_consistent(conn: Connection) -> None:
    owner = _user(conn)
    # ADR 0010 section 5: device and system findings are stored high.
    _refused(conn, lambda: _finding(conn, owner, rule_id="E1", tier="device"))
    # PLAN 7.1 step 7: backfill never notifies.
    _refused(conn, lambda: _finding(conn, owner, origin="backfill", notify_state="pending"))
    # sent exactly when notified_at is set; ended exactly when ended_at is set.
    _refused(conn, lambda: _finding(conn, owner, notify_state="sent"))
    _refused(conn, lambda: _finding(conn, owner, notified_at="2026-10-30T00:00:00Z"))
    _refused(conn, lambda: _finding(conn, owner, status="ended"))
    _refused(conn, lambda: _finding(conn, owner, ended_at="2026-10-30T00:00:00Z"))
    _refused(conn, lambda: _finding(conn, owner, severity="critical"))
    _refused(conn, lambda: _finding(conn, owner, confidence="certain"))
    _refused(conn, lambda: _finding(conn, owner, rule_id="c1"))
    _finding(conn, owner, notify_state="sent", notified_at="2026-10-30T00:00:00Z")


def _daily(conn: Connection, owner: uuid.UUID, **overrides: Any) -> None:
    row: dict[str, Any] = {
        "method": "morning",
        "value": 80.1,
        "morning": 80.1,
        "mean": 80.3,
        "min": 80.1,
        "max": 80.5,
        "mad": 0.2,
        "n": 2,
        "groups": [101],
        "day": "2026-10-28",
        **overrides,
    }
    conn.execute(
        text(
            "INSERT INTO metric_daily (owner_id, metric_key, day, method, value, morning, mean, "
            "min, max, mad, n, source_groups, algo_version, input_revision, input_fingerprint) "
            "VALUES (:owner, 'weight', :day, :method, :value, :morning, :mean, :min, :max, :mad, "
            ":n, :groups, 'daily/1', 3, :hash)"
        ),
        {"owner": owner, "hash": HASH, **row},
    )


def test_metric_daily_holds_the_daily_value(conn: Connection) -> None:
    owner = _user(conn)
    _daily(conn, owner)
    _daily(
        conn, owner, day="2026-10-29", method="median", value=80.3, morning=None, groups=[102, 103]
    )
    _refused(conn, lambda: _daily(conn, owner))  # one value per (owner, metric, day)
    _refused(conn, lambda: _daily(conn, owner, day="2026-10-30", morning=None))
    _refused(conn, lambda: _daily(conn, owner, day="2026-10-30", method="median"))
    _refused(conn, lambda: _daily(conn, owner, day="2026-10-30", value=80.2))
    _refused(conn, lambda: _daily(conn, owner, day="2026-10-30", min=81, max=80))
    _refused(conn, lambda: _daily(conn, owner, day="2026-10-30", mad=-0.1))
    _refused(conn, lambda: _daily(conn, owner, day="2026-10-30", groups=[]))
    _refused(conn, lambda: _daily(conn, owner, day="2026-10-30", groups=[1, 2]))
    _refused(conn, lambda: _daily(conn, owner, day="2026-10-30", method="mean"))
    _refused(conn, lambda: _daily(conn, owner, day="2026-10-30", value="NaN", morning="NaN"))


def test_scores_and_profiles_check_their_values(conn: Connection) -> None:
    owner = _user(conn)
    score = (
        "INSERT INTO scores (owner_id, kind, day, value, algo_version, input_revision, "
        "input_fingerprint) VALUES (:o, 'health_improvement', '2026-10-28', 62, :v, 1, :h)"
    )
    conn.execute(text(score), {"o": owner, "v": "score/1", "h": HASH})
    _refused(conn, lambda: conn.execute(text(score), {"o": owner, "v": "score/1", "h": HASH}))
    _refused(conn, lambda: conn.execute(text(score), {"o": owner, "v": "v1", "h": HASH}))
    conn.execute(text("INSERT INTO user_profiles (owner_id) VALUES (:o)"), {"o": owner})
    _refused(
        conn,
        lambda: conn.execute(
            text("INSERT INTO user_profiles (owner_id) VALUES (:o)"), {"o": owner}
        ),
    )
    other = _user(conn)
    conn.execute(text("INSERT INTO user_profiles (owner_id) VALUES (:o)"), {"o": other})
    for bad in (
        "birth_year = 1850",
        "sex = 'other'",
        "day_assignment = 'fixed_tz'",
        "day_tz = 'Europe/Stockholm'",
        "notify_tiers = '{device,body}'",
        "overview_order = '{body,heart,lungs}'",
        "weight_unit = 'g'",
    ):
        update = text(f"UPDATE user_profiles SET {bad} WHERE owner_id = :o")  # noqa: S608
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(update, {"o": other})
    conn.execute(
        text(
            "UPDATE user_profiles SET birth_year = 1980, sex = 'female', "
            "day_assignment = 'fixed_tz', day_tz = 'Europe/Stockholm' WHERE owner_id = :o"
        ),
        {"o": other},
    )


def _observation(conn: Connection, owner: uuid.UUID, **overrides: Any) -> None:
    row: dict[str, Any] = {
        "grpid": 501,
        "type": 174,
        "position": 2,
        "flags": [],
        "value": 3.2,
        **overrides,
    }
    conn.execute(
        text(
            "INSERT INTO observations (owner_id, metric_key, ts, source_ts, source_tz, value, "
            "source_value_raw, source_unit, conversion, quality_flags, source_grpid, "
            "source_type, source_position, algo_version, input_revision, input_fingerprint) "
            "VALUES (:owner, 'fat_mass_segment', '2026-10-28 06:30:00+00', "
            "'2026-10-28 06:30:00+00', 'Europe/Stockholm', :value, 3200, -3, 'scale:1e-3', "
            ":flags, :grpid, :type, :position, 'normalize/1', 1, :hash)"
        ),
        {"owner": owner, "hash": HASH, **row},
    )


def test_observations_key_segmental_repeats_on_grpid_and_position(
    conn: Connection,
) -> None:
    conn.execute(
        text(
            "CREATE TABLE public.observations_y2026m10 PARTITION OF public.observations "
            "FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')"
        )
    )
    owner = _user(conn)
    _observation(conn, owner)
    _observation(conn, owner, position=3)  # same group, other segment: kept
    _observation(conn, owner, grpid=502)  # a deliberate repeat measurement: kept
    _refused(conn, lambda: _observation(conn, owner))  # the same grpid and position
    _observation(conn, owner, grpid=None, type=None, position=None)
    _refused(conn, lambda: _observation(conn, owner, grpid=None, type=None, position=None))
    _refused(conn, lambda: _observation(conn, owner, grpid=None, position=7))
    _refused(conn, lambda: _observation(conn, owner, grpid=503, flags=["odd"]))
    _refused(conn, lambda: _observation(conn, owner, grpid=503, value="Infinity"))
    _observation(conn, owner, grpid=503, flags=["unconfirmed", "jump"])


def test_the_migration_writes_the_vocabularies_of_the_tables_module() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    def listed(values: tuple[str, ...]) -> bool:
        quoted = ", ".join(f"'{value}'" for value in values)
        return f"({quoted})" in source or f"ARRAY[{quoted}]" in source

    for values in (
        derived.QUALITY_FLAGS,
        derived.DAILY_METHODS,
        derived.TIERS,
        derived.SEVERITIES,
        derived.CONFIDENCES,
        derived.ORIGINS,
        derived.STATUSES,
        derived.NOTIFY_STATES,
        derived.SEXES,
        derived.DAY_ASSIGNMENTS,
        derived.THEMES,
        derived.WEIGHT_UNITS,
        derived.TEMPERATURE_UNITS,
        derived.DATETIME_FORMATS,
        derived.CHART_RANGES,
        derived.OVERVIEW_GROUPS,
    ):
        assert listed(values), values
    assert "'{{" + ",".join(derived.DEFAULT_NOTIFY_TIERS) + "}}'" in source
    assert f"(rule_id = '{derived.SYSTEM_RULE}')" in source
    assert re.search(r"_OWNED = \(([^)]*)\)", source)[1] == ", ".join(  # type: ignore[index]
        f'"{name}"' for name in DERIVED_OWNED
    )
    assert set(derived.metadata.tables) == set(DERIVED_OWNED)


def test_upgrade_and_downgrade_round_trip(
    database_url: str,
    app_database_url: str,
    owner_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OWNVITALS_MIGRATIONS_DATABASE_URL", owner_database_url)
    with engine_for(app_database_url) as engine, engine.begin() as connection:
        connection.execute(
            text(
                "SELECT ensure_month_partitions('observations', DATE '2026-09-01', "
                "DATE '2026-09-01')"
            )
        )
    config = alembic_config()
    command.downgrade(config, "0010_withings_rate")
    names = public_tables(owner_database_url)
    assert names.isdisjoint({*DERIVED_OWNED, "observations_y2026m09"})
    assert {"measures", "sleep_series", "data_revisions"} <= names
    command.upgrade(config, "0011_analysis_schema")
    assert set(DERIVED_OWNED) <= public_tables(owner_database_url)
    with engine_for(database_url) as engine, engine.connect() as connection:
        assert check_tables(connection, SYSTEM_TABLES) == []


def test_the_tables_module_names_the_migrated_columns(conn: Connection) -> None:
    for name in DERIVED_OWNED:
        migrated = set(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t"
                ),
                {"t": name},
            ).scalars()
        )
        assert migrated == set(derived.metadata.tables[name].columns.keys()), name
