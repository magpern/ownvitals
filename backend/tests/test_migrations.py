from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from dbtools import alembic_config, applied_revisions, migrated, public_tables


def test_the_revisions_form_one_line() -> None:
    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == ["0001_baseline"]
    assert script.get_base() == "0001_baseline"


def test_offline_mode_renders_sql_without_a_database(capsys: pytest.CaptureFixture[str]) -> None:
    command.upgrade(alembic_config(), "head", sql=True)
    out = capsys.readouterr().out
    assert "CREATE TABLE alembic_version" in out
    assert "0001_baseline" in out


def test_upgrade_head_then_downgrade_base(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # env.py takes the owner role's URL from Settings, so the test points it at the test database.
    monkeypatch.setenv("OWNVITALS_MIGRATIONS_DATABASE_URL", database_url)
    config = alembic_config()
    try:
        command.upgrade(config, "head")
        assert applied_revisions(database_url) == ["0001_baseline"]
        command.downgrade(config, "base")
        assert applied_revisions(database_url) == []
        command.upgrade(config, "head")  # the baseline is repeatable
        assert applied_revisions(database_url) == ["0001_baseline"]
    finally:
        command.downgrade(config, "base")


def test_the_baseline_creates_no_tables_of_its_own(database_url: str) -> None:
    with migrated(database_url):
        assert public_tables(database_url) == {"alembic_version"}
    assert applied_revisions(database_url) == []


def test_the_migrations_use_the_migration_url_and_its_password_file(
    database_url: str, monkeypatch: pytest.MonkeyPatch, secrets_dir: Path
) -> None:
    """Alembic connects with ``migrations_database_url`` and ``database_owner_password`` only;
    the app role's URL and password play no part."""
    url = make_url(database_url)
    assert url.password, "the test database URL must carry a password"
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
        assert applied_revisions(database_url) == ["0001_baseline"]
    finally:
        command.downgrade(config, "base")
