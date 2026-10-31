"""The settings of the platform milestone, declared ahead of the modules that read them, so the
issues building those modules add no field to ``config.py``."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ownvitals.config import SECRET_FILE_FIELDS, Settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.signup_mode == "invite"
    assert settings.stepup_window_minutes == 10
    assert settings.connection_kek_id == "kek-1"
    assert (settings.smtp_host, settings.smtp_port, settings.smtp_tls) == (None, 587, "starttls")
    assert settings.smtp_sender == "ownvitals@localhost"
    assert settings.oauth_google_client_id is None
    assert settings.oauth_google_client_secret is None
    assert (settings.job_lease_seconds, settings.worker_concurrency) == (120, 2)
    assert (settings.worker_maintenance_seconds, settings.worker_shutdown_wait_seconds) == (30, 25)
    assert (settings.jobs_per_user, settings.job_busy_requeue_limit) == (2, 5)
    assert settings.maint_database_url is None
    assert settings.maint_sqlalchemy_url() is None


def test_the_flags_are_off_unless_the_environment_turns_them_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (Settings().experimental_findings, Settings().ai_insights) == (False, False)
    monkeypatch.setenv("OWNVITALS_EXPERIMENTAL_FINDINGS", "true")
    monkeypatch.setenv("OWNVITALS_AI_INSIGHTS", "1")
    settings = Settings()
    assert settings.experimental_findings is True
    assert settings.ai_insights is True


def test_the_secret_files_are_exactly_the_documented_ones() -> None:
    assert set(SECRET_FILE_FIELDS) == {
        "database_password",
        "database_owner_password",
        "database_maint_password",
        "app_secret_key",
        "smtp_password",
        "oauth_google_client_secret",
    }
    assert set(SECRET_FILE_FIELDS) <= set(Settings.model_fields)


@pytest.mark.parametrize("name", SECRET_FILE_FIELDS)
def test_every_secret_comes_from_its_file_and_never_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    value = "x" * 40 + "-from-the-file"
    (tmp_path / name).write_text(value + "\n", encoding="utf-8")
    monkeypatch.setenv("OWNVITALS_SECRETS_DIR", str(tmp_path))
    assert getattr(Settings(), name).get_secret_value() == value
    (tmp_path / name).unlink()
    monkeypatch.setenv(f"OWNVITALS_{name.upper()}", "y" * 40 + "-from-the-environment")
    assert getattr(Settings(), name) is None


def test_secrets_stay_masked_when_they_come_from_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in SECRET_FILE_FIELDS:
        (tmp_path / name).write_text(f"planted-{name}-" + "z" * 32, encoding="utf-8")
    monkeypatch.setenv("OWNVITALS_SECRETS_DIR", str(tmp_path))
    settings = Settings(maint_database_url="postgresql+psycopg://maint:planted-url@db/ownvitals")  # pyright: ignore[reportArgumentType]
    shown = [
        repr(settings),
        str(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
        json.dumps(settings.model_dump(mode="json")),
    ]
    for text in shown:
        assert "planted-" not in text


def test_the_admin_role_url_needs_its_password_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(
        "OWNVITALS_MAINT_DATABASE_URL", "postgresql+psycopg://ownvitals_maint@db.example/ownvitals"
    )
    assert Settings().maint_sqlalchemy_url() is None  # no password file
    (tmp_path / "database_maint_password").write_text("", encoding="utf-8")
    monkeypatch.setenv("OWNVITALS_SECRETS_DIR", str(tmp_path))
    assert Settings().maint_sqlalchemy_url() is None  # an empty file counts as none
    (tmp_path / "database_maint_password").write_text("maint-secret\n", encoding="utf-8")
    maint = Settings().maint_sqlalchemy_url()
    assert maint is not None
    assert (maint.username, maint.password, maint.host) == (
        "ownvitals_maint",
        "maint-secret",
        "db.example",
    )


def test_the_maint_url_is_validated_without_echoing_it() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(maint_database_url="mysql://u:leaky-secret@h/db")  # pyright: ignore[reportArgumentType]
    assert "maint_database_url" in str(excinfo.value)
    assert "leaky-secret" not in str(excinfo.value)


@pytest.mark.parametrize("value", ["short", "x" * 31])
def test_app_secret_key_must_be_at_least_32_bytes(value: str) -> None:
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(app_secret_key=value)  # pyright: ignore[reportArgumentType]
    assert Settings(app_secret_key="x" * 32)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("value", ["kek-1", "kek-2026-10", "kek-" + "a" * 28])
def test_connection_kek_id_accepts_the_documented_form(value: str) -> None:
    assert Settings(connection_kek_id=value).connection_kek_id == value


@pytest.mark.parametrize("value", ["", "kek", "kek-", "KEK-1", "kek-1_", "kek-" + "a" * 29, "k-1"])
def test_connection_kek_id_rejects_other_forms(value: str) -> None:
    with pytest.raises(ValidationError, match="connection_kek_id"):
        Settings(connection_kek_id=value)


@pytest.mark.parametrize("mode", ["invite", "closed", "open"])
def test_signup_mode_takes_the_three_modes(mode: str) -> None:
    assert Settings(signup_mode=mode).signup_mode == mode  # pyright: ignore[reportArgumentType]


def test_signup_mode_rejects_anything_else() -> None:
    with pytest.raises(ValidationError):
        Settings(signup_mode="public")  # pyright: ignore[reportArgumentType]


def test_admin_emails_are_normalised() -> None:
    settings = Settings(admin_emails=" Owner@Example.COM , second@example.com,,")
    assert settings.admin_email_set == {"owner@example.com", "second@example.com"}


@pytest.mark.parametrize("host", ["localhost", "mailpit", "127.0.0.1", "::1", "[::1]"])
def test_plain_smtp_is_allowed_for_local_hosts(host: str) -> None:
    assert Settings(smtp_host=host, smtp_tls="none").smtp_tls == "none"


@pytest.mark.parametrize("host", ["smtp.example.com", "10.0.0.5", "203.0.113.9"])
def test_plain_smtp_is_refused_for_any_other_host(host: str) -> None:
    with pytest.raises(ValidationError, match="smtp_tls=none"):
        Settings(smtp_host=host, smtp_tls="none")
    assert Settings(smtp_host=host, smtp_tls="starttls").smtp_host == host


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_smtp_port_is_a_port(port: int) -> None:
    with pytest.raises(ValidationError, match="smtp_port"):
        Settings(smtp_port=port)


@pytest.mark.parametrize(
    ("name", "good", "bad"),
    [
        ("job_lease_seconds", (30, 600), (29, 601)),
        ("worker_concurrency", (1, 16), (0, 17)),
        ("jobs_per_user", (1, 100), (0, 101)),
        ("job_busy_requeue_limit", (0, 100), (-1, 101)),
        ("worker_shutdown_wait_seconds", (0, 25), (-1, 26)),
        ("stepup_window_minutes", (1, 60), (0, 61)),
    ],
)
def test_numeric_settings_have_bounds(
    name: str, good: tuple[int, int], bad: tuple[int, int]
) -> None:
    for value in good:
        assert getattr(Settings(**{name: value}), name) == value  # pyright: ignore[reportArgumentType]
    for value in bad:
        with pytest.raises(ValidationError):
            Settings(**{name: value})  # pyright: ignore[reportArgumentType]


def test_the_worker_maintenance_interval_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(worker_maintenance_seconds=0)
