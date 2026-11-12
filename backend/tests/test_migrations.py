import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError

from dbtools import (
    alembic_config,
    applied_revisions,
    drop_queue,
    migrated,
    public_tables,
    ready_to_downgrade,
)
from jobtools import ensure_user
from ownvitals.derived.tables import user_profiles

HEAD = "0029_session_anomaly"


def test_the_revisions_form_one_line() -> None:
    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == [HEAD]
    assert script.get_base() == "0001_baseline"
    assert [r.revision for r in script.walk_revisions()] == [
        HEAD,
        "0028_chart_range_7d",
        "0027_ai_conversation_retention",
        "0026_ai_digest",
        "0025_ai_job_failures",
        "0024_outbox_export_ready",
        "0023_ai_audit",
        "0022_exports",
        "0021_ai_consent",
        "0020_notify_holds",
        "0019_auth_event_origin",
        "0018_cross_device_session",
        "0017_account_locale",
        "0016_findings_rule_ids",
        "0015_qr_login",
        "0014_passkeys",
        "0013_ai_budget",
        "0012_outbox",
        "0011_analysis_schema",
        "0010_withings_rate",
        "0009_withings_schema",
        "0008_withings_jobs",
        "0007_connections",
        "0006_admin",
        "0005_auth",
        "0004_connection_keks",
        "0003_jobs",
        "0002_close_alembic_version",
        "0001_baseline",
    ]


def test_offline_mode_renders_sql_without_a_database(capsys: pytest.CaptureFixture[str]) -> None:
    command.upgrade(alembic_config(), "head", sql=True)
    out = capsys.readouterr().out
    assert "CREATE TABLE alembic_version" in out
    assert "0001_baseline" in out
    assert "REVOKE ALL ON TABLE public.alembic_version FROM ownvitals_app, PUBLIC" in out


def test_upgrade_head_then_downgrade_base(
    database_url: str, owner_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # env.py takes the owner role's URL from Settings, so the test points it at the owner role.
    monkeypatch.setenv("OWNVITALS_MIGRATIONS_DATABASE_URL", owner_database_url)
    config = alembic_config()
    try:
        ready_to_downgrade(owner_database_url)
        command.upgrade(config, "head")
        assert applied_revisions(database_url) == [HEAD]
        ready_to_downgrade(owner_database_url)
        command.downgrade(config, "base")
        assert applied_revisions(database_url) == []
        command.upgrade(config, "head")  # the revisions are repeatable
        assert applied_revisions(database_url) == [HEAD]
    finally:
        ready_to_downgrade(owner_database_url)
        command.downgrade(config, "base")


def test_the_migrations_create_exactly_their_tables(
    database_url: str, owner_database_url: str
) -> None:
    """Besides Alembic's own table, 0003's job tables (ADR 0005 section 1), 0004's key-ring
    check table (ADR 0004 section 3), 0005's account tables (ADR 0003, #10), 0006's stored
    flags and admin audit log (#12), 0007's connections table (#13), 0009's Withings tables,
    0011's derived tables (#181), 0012's mail outbox (#194), 0013's AI cost cap (#205),
    0020's notification holds (#195), 0021's AI consent and history (#206), 0022's exports
    (#211), 0023's AI audit log (#207) and 0026's weekly digest (issue 209).
    """
    from ownvitals.ai.audit import AI_AUDIT_OWNED
    from ownvitals.ai.budget import AI_OWNED
    from ownvitals.ai.tables import AI_CONSENT_OWNED, AI_DIGEST_OWNED
    from ownvitals.auth.tables import metadata
    from ownvitals.derived.tables import DERIVED_OWNED
    from ownvitals.ingest.tables import INGEST_OWNED
    from ownvitals.notify.outbox import OUTBOX_TABLES
    from ownvitals.notify.tables import NOTIFY_OWNED
    from ownvitals.reports.tables import EXPORT_OWNED
    from ownvitals.withings.tables import WITHINGS_OWNED

    with migrated(owner_database_url):
        assert public_tables(database_url) == {
            "alembic_version",
            "jobs",
            "job_events",
            "connection_keks",
            "app_settings",
            "admin_audit",
            "connections",
            "withings_apps",
            "webhook_events",
            "withings_rate_buckets",
            "withings_rate_slots",
            "withings_rate_demand",
            "ai_spend_global",
            *AI_OWNED,
            *metadata.tables,
            *WITHINGS_OWNED,
            *INGEST_OWNED,
            *DERIVED_OWNED,
            *OUTBOX_TABLES,
            *NOTIFY_OWNED,
            *AI_CONSENT_OWNED,
            *EXPORT_OWNED,
            *AI_AUDIT_OWNED,
            *AI_DIGEST_OWNED,
        }
    assert applied_revisions(database_url) == []
    assert public_tables(database_url) == {"alembic_version"}  # downgraded: all of them gone


def test_downgrading_0004_is_refused_while_it_holds_check_rows(
    database_url: str, owner_database_url: str
) -> None:
    """A start records the current keys' check rows only, so a retired key's row could never
    be recreated after the table was dropped (0004's docstring)."""
    engine = create_engine(database_url)
    try:
        with migrated(owner_database_url):
            with engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO connection_keks (key_id, check_value) VALUES ('kek-0', :v)"),
                    {"v": bytes(16)},
                )
            drop_queue()  # the check rows must stay: they are what the downgrade refuses
            with pytest.raises(DBAPIError, match="connection_keks holds check rows"):
                command.downgrade(alembic_config(), "0003_jobs")
            assert applied_revisions(database_url) == [HEAD]
            assert "connection_keks" in public_tables(database_url)
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM connection_keks"))
            command.downgrade(alembic_config(), "0003_jobs")  # empty: dropped
            assert applied_revisions(database_url) == ["0003_jobs"]
            assert "connection_keks" not in public_tables(database_url)
            assert {"jobs", "job_events"} <= public_tables(database_url)
    finally:
        engine.dispose()
    assert applied_revisions(database_url) == []


def test_the_app_role_cannot_read_or_change_the_migration_state(
    owner_database_url: str, app_database_url: str
) -> None:
    """0002 closes ``alembic_version`` to the app role: the owner's default privileges had
    given it DML when Alembic created the table."""
    with migrated(owner_database_url):
        engine = create_engine(app_database_url)
        try:
            for statement in (
                "SELECT version_num FROM alembic_version",
                "UPDATE alembic_version SET version_num = '0001_baseline'",
                "DELETE FROM alembic_version",
            ):
                with (
                    engine.connect() as connection,
                    pytest.raises(ProgrammingError, match="permission denied"),
                ):
                    connection.execute(text(statement))
        finally:
            engine.dispose()


def test_the_migrations_use_the_migration_url_and_its_password_file(
    database_url: str,
    owner_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    secrets_dir: Path,
) -> None:
    """Alembic connects with ``migrations_database_url`` and ``database_owner_password`` only;
    the app role's URL and password play no part."""
    url = make_url(owner_database_url)
    assert url.password, "the owner URL must carry a password"
    (secrets_dir / "database_owner_password").write_text(url.password + "\n", encoding="utf-8")
    monkeypatch.setenv(
        "OWNVITALS_MIGRATIONS_DATABASE_URL",
        url.set(password="wrong").render_as_string(hide_password=False),
    )
    monkeypatch.setenv(
        "OWNVITALS_DATABASE_URL",
        url.set(password="wrong-too").render_as_string(hide_password=False),
    )
    config = alembic_config()
    try:
        command.upgrade(config, "head")  # the password comes from the owner file
        assert applied_revisions(database_url) == [HEAD]
    finally:
        ready_to_downgrade(owner_database_url)
        command.downgrade(config, "base")


def _profile_rows(database_url: str, ranges: list[str | None]) -> list[uuid.UUID]:
    """Synthetic owners with a profile each; ``None`` leaves ``chart_range`` to its default."""
    owners = [uuid.uuid4() for _ in ranges]
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            for owner, chart_range in zip(owners, ranges, strict=True):
                ensure_user(connection, owner)
                if chart_range is None:
                    connection.execute(
                        text("INSERT INTO user_profiles (owner_id) VALUES (:o)"), {"o": owner}
                    )
                else:
                    connection.execute(
                        text("INSERT INTO user_profiles (owner_id, chart_range) VALUES (:o, :r)"),
                        {"o": owner, "r": chart_range},
                    )
    finally:
        engine.dispose()
    return owners


def _chart_ranges(database_url: str, owners: list[uuid.UUID]) -> list[str]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            found = dict(
                connection.execute(
                    text(
                        "SELECT owner_id, chart_range FROM user_profiles WHERE owner_id = ANY(:o)"
                    ),
                    {"o": owners},
                ).all()
            )
    finally:
        engine.dispose()
    return [found[owner] for owner in owners]


def test_a_new_profile_row_defaults_to_7_days_and_a_saved_value_stays(
    database_url: str, owner_database_url: str
) -> None:
    """The column default is ``7d`` (0028); an explicit ``90d`` is kept."""
    with migrated(owner_database_url):
        owners = _profile_rows(database_url, [None, "90d"])
        assert _chart_ranges(database_url, owners) == ["7d", "90d"]


def test_downgrading_0028_maps_7d_rows_to_30d_and_restores_the_narrow_check(
    database_url: str, owner_database_url: str
) -> None:
    with migrated(owner_database_url):
        owners = _profile_rows(database_url, ["7d", None, "90d", "1y"])
        command.downgrade(alembic_config(), "0027_ai_conversation_retention")
        assert applied_revisions(database_url) == ["0027_ai_conversation_retention"]
        # The 7d rows (saved and defaulted) became 30d; the others are untouched.
        assert _chart_ranges(database_url, owners) == ["30d", "30d", "90d", "1y"]
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM user_profiles WHERE owner_id = ANY(:o)"), {"o": owners}
                )
            with pytest.raises(DBAPIError, match="user_profiles_chart_range"), engine.begin() as c:
                c.execute(
                    text("INSERT INTO user_profiles (owner_id, chart_range) VALUES (:o, '7d')"),
                    {"o": owners[0]},
                )
            with engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO user_profiles (owner_id) VALUES (:o)"), {"o": owners[0]}
                )
                default = connection.execute(
                    text("SELECT chart_range FROM user_profiles WHERE owner_id = :o"),
                    {"o": owners[0]},
                ).scalar_one()
            assert default == "90d"
        finally:
            engine.dispose()
        command.upgrade(alembic_config(), "head")  # the revision is repeatable
        assert applied_revisions(database_url) == [HEAD]


def test_the_model_default_of_the_chart_range_is_the_migrated_default(
    database_url: str, owner_database_url: str
) -> None:
    """``derived/tables.py`` and migration 0028 state the default twice; they must agree."""
    declared = user_profiles.c.chart_range.server_default
    assert declared is not None
    with migrated(owner_database_url):
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                actual = connection.execute(
                    text(
                        "SELECT column_default FROM information_schema.columns WHERE "
                        "table_schema = 'public' AND table_name = 'user_profiles' "
                        "AND column_name = 'chart_range'"
                    )
                ).scalar_one()
        finally:
            engine.dispose()
    assert str(declared.arg) == "'7d'"  # type: ignore[union-attr]
    assert actual.split("::")[0] == str(declared.arg)  # type: ignore[union-attr]
