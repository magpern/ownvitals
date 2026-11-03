"""The derived schema (#181): guard 1 on the new tables, the episode index, the system tier,
the check constraints, the vocabularies and the downgrade. Synthetic rows only; no real
account or measurement.
"""

# Each refusal runs in a nested transaction inside pytest.raises so it rolls back alone.

import json
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
        "metric_keys": ["resting_hr"],
        **overrides,
    }
    return conn.execute(
        text(
            "INSERT INTO findings (owner_id, rule_id, rule_version, tier, severity, confidence, "
            "origin, subject, window_start, window_end, input_revision, input_fingerprint, "
            "status, ended_at, notify_state, notified_at, metric_keys) VALUES (:owner, :rule_id, "
            ":rule_version, :tier, :severity, :confidence, :origin, :subject, '2026-10-01', "
            "'2026-10-28', 0, :hash, :status, :ended_at, :notify_state, :notified_at, "
            "CAST(:metric_keys AS text[])) RETURNING id"
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
        "key": "weight",
        **overrides,
    }
    conn.execute(
        text(
            "INSERT INTO metric_daily (owner_id, metric_key, day, method, value, morning, mean, "
            "min, max, mad, n, source_groups, algo_version, input_revision, input_fingerprint) "
            "VALUES (:owner, :key, :day, :method, :value, :morning, :mean, :min, :max, :mad, "
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


def _score(conn: Connection, owner: uuid.UUID, **overrides: Any) -> None:
    row: dict[str, Any] = {
        "kind": "his",
        "day": "2026-10-28",
        "value": 62,
        "status": "computed",
        "version": "his/1",
        **overrides,
    }
    # ``components`` (raw JSON) replaces the status object, so a test can omit the status.
    components = row.pop("components", None)
    if components is None:
        components = json.dumps({"status": row["status"]})
    conn.execute(
        text(
            "INSERT INTO scores (owner_id, kind, day, value, components, algo_version, "
            "input_revision, input_fingerprint) VALUES (:owner, :kind, :day, :value, "
            "CAST(:components AS jsonb), :version, 1, :hash)"
        ),
        {
            "owner": owner,
            "hash": HASH,
            "components": components,
            **{k: v for k, v in row.items() if k != "status"},
        },
    )


def test_scores_take_adr_0012_kinds_and_a_value_only_when_computed(conn: Connection) -> None:
    owner = _user(conn)
    for kind in ("his", "readiness", "body_path:weight", "body_path:muscle_mass:left_arm"):
        _score(conn, owner, kind=kind)
    # ADR 0012 section 1: every status but computed stores value NULL.
    for day, status in enumerate(derived.SCORE_STATUSES[1:], start=1):
        _score(conn, owner, day=f"2026-10-{day:02d}", value=None, status=status)
    _refused(conn, lambda: _score(conn, owner, day="2026-10-20", value=None))
    _refused(conn, lambda: _score(conn, owner, day="2026-10-20", status="not_set"))
    _refused(conn, lambda: _score(conn, owner, day="2026-10-20", status="normal"))
    for kind in ("health_improvement", "body_path", "body_path:", "body_path:Weight", "his:x"):
        _refused(conn, lambda kind=kind: _score(conn, owner, day="2026-10-20", kind=kind))


@pytest.mark.parametrize(
    ("components", "value"),
    [
        ("{}", 62),  # no status, with a value
        ("{}", None),  # no status, no value
        ('{"status": null}', None),  # a JSON-null status
        ('{"status": null}', 62),
        ('{"status": 1}', 62),  # not a string
        ('{"status": ["computed"]}', 62),
        ('{"status": "computed"}', None),  # computed needs its value
        ('{"status": ""}', None),
        ('{"status": "Computed"}', 62),
    ],
)
def test_scores_status_is_null_proof(conn: Connection, components: str, value: Any) -> None:
    """A CHECK that evaluates to NULL passes in Postgres, so a missing or JSON-null status must
    still be refused (the #216 review's N1)."""
    owner = _user(conn)
    _refused(conn, lambda: _score(conn, owner, components=components, value=value))


@pytest.mark.parametrize("status", derived.SCORE_STATUSES[1:])
def test_a_score_that_is_not_computed_has_no_value(conn: Connection, status: str) -> None:
    owner = _user(conn)
    _refused(conn, lambda: _score(conn, owner, status=status))
    _score(conn, owner, status=status, value=None)


GOOD_KEYS = ("weight", "segmental_muscle_mass:left_arm", "qtc_interval", "fat_mass:right_leg")
BAD_KEYS = ("Weight", "weight:", ":left_arm", "a:b:c", "weight:Left", "fat mass", "1weight",
            "weight;drop", "segmental_muscle_mass:left_arm_" + "x" * 30)  # fmt: skip


def test_metric_keys_take_one_optional_segment(conn: Connection) -> None:
    """ADR 0010's segmental keys (``segmental_muscle_mass:left_arm``) in every metric-key
    column: observations, metric_daily and findings.metric_keys; nothing else."""
    assert all(re.fullmatch(derived.METRIC_KEY_PATTERN, k) for k in GOOD_KEYS)
    assert not any(re.fullmatch(derived.METRIC_KEY_PATTERN, k) for k in BAD_KEYS)
    owner = _user(conn)
    conn.execute(
        text(
            "CREATE TABLE public.observations_y2026m10 PARTITION OF public.observations "
            "FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')"
        )
    )
    for n, key in enumerate(GOOD_KEYS):
        _observation(conn, owner, key=key)
        _daily(conn, owner, key=key)
        _finding(conn, owner, subject=f"s{n}", metric_keys=[key])
    _finding(conn, owner, subject="all", metric_keys=list(GOOD_KEYS))
    for key in BAD_KEYS:
        _refused(conn, lambda key=key: _observation(conn, owner, key=key))
        _refused(conn, lambda key=key: _daily(conn, owner, key=key))
        _refused(conn, lambda key=key: _finding(conn, owner, subject="bad", metric_keys=[key]))
        _refused(
            conn,
            lambda key=key: _finding(conn, owner, subject="bad", metric_keys=["weight", key]),
        )
    _refused(conn, lambda: _finding(conn, owner, subject="bad", metric_keys=["weight", ""]))
    _finding(conn, owner, subject="none", metric_keys=[])  # a Q1-style finding: no metric
    _refused(
        conn,
        lambda: _finding(conn, owner, subject="many", metric_keys=[f"k{n}" for n in range(17)]),
    )


@pytest.mark.parametrize(
    "keys",
    [
        ["weight,bmi"],  # one element holding a comma: two valid keys once joined
        ["weight,Bmi"],
        ["weight", "fat_mass,bmi"],
        [""],  # a lone empty element joins to ''
        ["", ""],
        ["", "weight"],
        [None],  # the join skips NULLs
        ["weight", None],
        [None, None],
    ],
)
def test_every_metric_keys_element_is_checked(conn: Connection, keys: list[str | None]) -> None:
    """Each element is a metric key on its own, not just the joined string (the #216 review's
    N2)."""
    owner = _user(conn)
    _refused(conn, lambda: _finding(conn, owner, metric_keys=keys))


@pytest.mark.parametrize(
    ("table", "assignment"),
    [
        ("user_profiles", "notify_tiers = ARRAY[NULL]::text[]"),
        ("user_profiles", "notify_tiers = ARRAY['device', NULL]"),
        ("user_profiles", "notify_tiers = ARRAY['']"),
        ("user_profiles", "overview_order = ARRAY[NULL]::text[]"),
        ("user_profiles", "overview_order = ARRAY['']"),
        ("user_profiles", "day_assignment = 'fixed_tz', day_tz = NULL"),
        ("user_profiles", "day_assignment = 'reading_tz', day_tz = 'Europe/Stockholm'"),
        ("user_profiles", "day_assignment = 'fixed_tz', day_tz = ''"),
    ],
)
def test_profile_checks_refuse_null_and_empty_elements(
    conn: Connection, table: str, assignment: str
) -> None:
    """The NULL-trap audit of the nullable and array columns of ``user_profiles``."""
    owner = _user(conn)
    conn.execute(text("INSERT INTO user_profiles (owner_id) VALUES (:o)"), {"o": owner})
    update = text(f"UPDATE {table} SET {assignment} WHERE owner_id = :o")  # noqa: S608
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update, {"o": owner})


def test_derived_null_traps_are_closed(conn: Connection) -> None:
    """The NULL-trap audit of the other nullable and array columns: a NULL element, a NULL in
    a nullable column a check reads, and the source reference without its group."""
    owner = _user(conn)
    conn.execute(
        text(
            "CREATE TABLE public.observations_y2026m10 PARTITION OF public.observations "
            "FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')"
        )
    )
    _refused(conn, lambda: _observation(conn, owner, flags=[None]))
    _refused(conn, lambda: _observation(conn, owner, flags=["jump", None]))
    _refused(conn, lambda: _observation(conn, owner, flags=[""]))
    _refused(conn, lambda: _observation(conn, owner, grpid=None, position=None))  # type alone
    _refused(conn, lambda: _daily(conn, owner, groups=[None]))
    _refused(conn, lambda: _daily(conn, owner, method="median", value=80.3, groups=[1, None]))
    _refused(conn, lambda: _daily(conn, owner, morning=None))  # morning method, no morning
    _refused(conn, lambda: _daily(conn, owner, method="median", value=80.3, morning=80.3))
    _refused(conn, lambda: _daily(conn, owner, morning="NaN", value="NaN"))
    _refused(conn, lambda: _finding(conn, owner, status="ended", ended_at=None))
    _refused(conn, lambda: _finding(conn, owner, notify_state="sent", notified_at=None))


def test_scores_and_profiles_check_their_values(conn: Connection) -> None:
    owner = _user(conn)
    _score(conn, owner)
    _refused(conn, lambda: _score(conn, owner))  # one row per (owner, kind, day)
    _refused(conn, lambda: _score(conn, owner, day="2026-10-29", version="v1"))
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
        "key": "fat_mass_segment",
        **overrides,
    }
    conn.execute(
        text(
            "INSERT INTO observations (owner_id, metric_key, ts, source_ts, source_tz, value, "
            "source_value_raw, source_unit, conversion, quality_flags, source_grpid, "
            "source_type, source_position, algo_version, input_revision, input_fingerprint) "
            "VALUES (:owner, :key, '2026-10-28 06:30:00+00', "
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
    assert f'_KEY_BODY = "{derived.METRIC_KEY_PATTERN[1:-1]}"' in source
    statuses = re.search(r"components->>'status' IN \(([^)]*)\)", source)
    assert statuses is not None
    assert re.findall(r"'([a-z_]+)'", statuses[1]) == list(derived.SCORE_STATUSES)
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
