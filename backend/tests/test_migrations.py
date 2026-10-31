from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

from dbtools import alembic_config, applied_revisions, migrated, public_tables

HEAD = "0003_connection_keks"


def test_the_revisions_form_one_line() -> None:
    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == [HEAD]
    assert script.get_base() == "0001_baseline"
    assert [r.revision for r in script.walk_revisions()] == [
        HEAD,
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
        command.upgrade(config, "head")
        assert applied_revisions(database_url) == [HEAD]
        command.downgrade(config, "base")
        assert applied_revisions(database_url) == []
        command.upgrade(config, "head")  # the revisions are repeatable
        assert applied_revisions(database_url) == [HEAD]
    finally:
        command.downgrade(config, "base")


def test_the_migrations_create_exactly_their_tables(
    database_url: str, owner_database_url: str
) -> None:
    with migrated(owner_database_url):
        # Alembic's own and the key ring's check table (0003, ADR 0004 section 3).
        assert public_tables(database_url) == {"alembic_version", "connection_keks"}
    assert applied_revisions(database_url) == []
    assert public_tables(database_url) == {"alembic_version"}  # downgraded: 0003's table gone


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
        command.downgrade(config, "base")
