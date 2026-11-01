import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from ownvitals.config import Settings, read_secret_files

URL = "postgresql+psycopg://app:url-password@db.example:5433/ownvitals"
PASSWORDLESS_URL = "postgresql+psycopg://app@db.example:5433/ownvitals"


def test_defaults() -> None:
    settings = Settings()
    assert settings.environment == "dev"
    assert settings.log_level == "INFO"
    assert settings.database_password is None
    assert settings.static_dir is None
    assert settings.worker_poll_seconds == 2
    assert settings.sqlalchemy_url().drivername == "postgresql+psycopg"


def test_reads_environment_with_prefix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OWNVITALS_ENVIRONMENT", "prod")
    monkeypatch.setenv("OWNVITALS_PUBLIC_BASE_URL", "https://ownvitals.example")
    monkeypatch.setenv("OWNVITALS_LOG_LEVEL", "debug")
    monkeypatch.setenv("OWNVITALS_DATABASE_URL", PASSWORDLESS_URL)  # prod: no password in a URL
    monkeypatch.setenv("OWNVITALS_STATIC_DIR", str(tmp_path))
    monkeypatch.setenv("OWNVITALS_WORKER_POLL_SECONDS", "0.5")
    monkeypatch.setenv("ENVIRONMENT", "dev")  # unprefixed names are ignored
    settings = Settings()
    assert settings.environment == "prod"
    assert settings.log_level == "DEBUG"
    assert settings.database_url.get_secret_value() == PASSWORDLESS_URL
    assert settings.static_dir == tmp_path
    assert settings.worker_poll_seconds == 0.5


def test_empty_environment_value_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNVITALS_STATIC_DIR", "")
    assert Settings().static_dir is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("environment", "staging"),
        ("log_level", "LOUD"),
        ("worker_poll_seconds", 0),
        ("worker_poll_seconds", -1),
    ],
)
def test_rejects_invalid_values(name: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(**{name: value})  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:leaky-secret@h/db",
        "mysql+pymysql://u:leaky-secret@h/db",
        "not a url, only leaky-secret",
    ],
)
def test_rejects_other_database_urls_without_echoing_them(url: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(database_url=url)  # pyright: ignore[reportArgumentType]
    assert "database_url" in str(excinfo.value)
    assert "leaky-secret" not in str(excinfo.value)


def test_password_from_secret_file_replaces_the_one_in_the_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "database_password").write_text("from-the-file\n", encoding="utf-8")
    monkeypatch.setenv("OWNVITALS_SECRETS_DIR", str(tmp_path))
    url = Settings(database_url=URL).sqlalchemy_url()  # pyright: ignore[reportArgumentType]
    assert url.password == "from-the-file"
    assert (url.username, url.host, url.port, url.database) == (
        "app",
        "db.example",
        5433,
        "ownvitals",
    )
    assert url.drivername == "postgresql+psycopg"


def test_database_password_is_not_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OWNVITALS_SECRETS_DIR", str(tmp_path))  # empty: no secret files
    monkeypatch.setenv("OWNVITALS_DATABASE_URL", URL)
    monkeypatch.setenv("OWNVITALS_DATABASE_PASSWORD", "from-the-env")
    settings = Settings()
    assert settings.database_password is None
    assert settings.sqlalchemy_url().password == "url-password"


def test_the_secret_file_name_has_no_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "ownvitals_database_password").write_text("prefixed", encoding="utf-8")
    monkeypatch.setenv("OWNVITALS_SECRETS_DIR", str(tmp_path))
    assert Settings().database_password is None


def test_url_password_is_kept_without_database_password() -> None:
    settings = Settings(database_url=URL)  # pyright: ignore[reportArgumentType]
    assert settings.sqlalchemy_url().password == "url-password"


def test_special_characters_in_the_password_survive_rendering() -> None:
    password = "p@ss:w/rd#1 %"
    settings = Settings(
        database_url=URL,  # pyright: ignore[reportArgumentType]
        database_password=password,  # pyright: ignore[reportArgumentType]
    )
    rendered = settings.sqlalchemy_url().render_as_string(hide_password=False)
    assert make_url(rendered).password == password


def test_secrets_do_not_appear_in_repr_str_or_dumps() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://app:url-secret@db.example/ownvitals",  # pyright: ignore[reportArgumentType]
        database_password="file-secret",  # pyright: ignore[reportArgumentType]
    )
    shown = [
        repr(settings),
        str(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
        json.dumps(settings.model_dump(mode="json")),
        repr(settings.sqlalchemy_url()),
        str(settings.sqlalchemy_url()),
    ]
    for text in shown:
        assert "url-secret" not in text
        assert "file-secret" not in text


def test_read_secret_files_ignores_missing_files_and_directories(tmp_path: Path) -> None:
    (tmp_path / "present").write_text("  value \n", encoding="utf-8")
    assert read_secret_files(tmp_path, ("present", "absent")) == {"present": "  value "}
    assert read_secret_files(tmp_path / "no-such-dir", ("present",)) == {}


def test_missing_secrets_dir_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OWNVITALS_SECRETS_DIR", str(tmp_path / "missing"))
    assert Settings().database_password is None


@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_secret_file_keeps_spaces_and_drops_only_the_trailing_newline(
    tmp_path: Path, ending: str
) -> None:
    (tmp_path / "database_password").write_bytes(f"  pa ss  {ending}".encode())
    assert read_secret_files(tmp_path, ("database_password",)) == {"database_password": "  pa ss  "}


def test_migrations_url_and_owner_password_are_separate_from_the_app_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "database_owner_password").write_text("owner-secret\n", encoding="utf-8")
    (tmp_path / "database_password").write_text("app-secret\n", encoding="utf-8")
    monkeypatch.setenv("OWNVITALS_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("OWNVITALS_DATABASE_URL", URL)
    monkeypatch.setenv(
        "OWNVITALS_MIGRATIONS_DATABASE_URL",
        "postgresql+psycopg://ownvitals_owner@db.example/ownvitals",
    )
    monkeypatch.setenv("OWNVITALS_DATABASE_OWNER_PASSWORD", "from-the-env")  # ignored
    settings = Settings()
    assert settings.sqlalchemy_url().password == "app-secret"
    assert settings.sqlalchemy_url().username == "app"
    migrations = settings.migrations_sqlalchemy_url()
    assert (migrations.username, migrations.password) == ("ownvitals_owner", "owner-secret")
    assert "owner-secret" not in repr(settings)
    assert "owner-secret" not in str(settings.model_dump())


def test_default_users_are_the_app_and_owner_roles() -> None:
    settings = Settings()
    assert settings.sqlalchemy_url().username == "ownvitals_app"
    assert settings.migrations_sqlalchemy_url().username == "ownvitals_owner"


def test_migrations_url_is_validated_without_echoing_it() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(migrations_database_url="mysql://u:leaky-secret@h/db")  # pyright: ignore[reportArgumentType]
    assert "migrations_database_url" in str(excinfo.value)
    assert "leaky-secret" not in str(excinfo.value)


def test_account_settings_defaults() -> None:
    settings = Settings()
    assert [str(n) for n in settings.trusted_proxy_networks] == ["127.0.0.0/8", "::1/128"]
    assert settings.admin_email_set == frozenset()
    assert settings.trusted_proxy_networks == ()
    assert (settings.session_idle_days, settings.session_absolute_days) == (7, 30)
    assert settings.password_hash_workers == 4
    assert settings.purge_interval_seconds == 3600


@pytest.mark.parametrize(
    ("value", "origin"),
    [
        ("https://Ownvitals.Example/", "https://ownvitals.example"),
        ("http://127.0.0.1:20036", "http://127.0.0.1:20036"),
        ("https://ownvitals.example:8443", "https://ownvitals.example:8443"),
    ],
)
def test_public_base_url_is_normalised_to_an_origin(value: str, origin: str) -> None:
    assert Settings(public_base_url=value).public_base_url == origin


@pytest.mark.parametrize(
    "value", ["ownvitals.example", "ftp://x", "https://x/path", "https://x?q=1", "https://u:p@x"]
)
def test_public_base_url_must_be_a_bare_http_origin(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(public_base_url=value)


def test_trusted_proxies_and_numeric_settings_are_validated() -> None:
    settings = Settings(trusted_proxies="10.0.0.0/8, 2001:db8::/32,192.0.2.1")
    assert len(settings.trusted_proxy_networks) == 3
    for name, value in (
        ("trusted_proxies", "not-an-ip"),
        ("cf_connecting_ip_proxies", "not-an-ip"),
        ("session_idle_days", 0),
        ("session_absolute_days", 0),
        ("password_hash_workers", 0),
        ("purge_interval_seconds", 0),
    ):
        with pytest.raises(ValidationError):
            Settings(**{name: value})  # pyright: ignore[reportArgumentType]


def test_trusted_proxies_default_to_loopback_and_cf_header_to_nobody() -> None:
    """ADR 0003 section 8: by default only a loopback peer is a trusted proxy, and no peer's
    ``CF-Connecting-IP`` is believed."""
    settings = Settings()
    assert [str(n) for n in settings.trusted_proxy_networks] == ["127.0.0.0/8", "::1/128"]
    assert settings.cf_connecting_ip_networks == ()
    swag = Settings(trusted_proxies="192.168.1.151", cf_connecting_ip_proxies="192.168.1.151")
    assert [str(n) for n in swag.cf_connecting_ip_networks] == ["192.168.1.151/32"]


def test_default_ports_are_dropped_from_the_public_origin() -> None:
    assert Settings(public_base_url="https://ownvitals.example:443").public_base_url == (
        "https://ownvitals.example"
    )
    assert Settings(public_base_url="http://localhost:80/").public_base_url == "http://localhost"
    assert Settings(public_base_url="https://x.example:80").public_base_url == (
        "https://x.example:80"
    )


def test_prod_needs_an_https_public_base_url() -> None:
    with pytest.raises(ValidationError, match="https"):
        Settings(environment="prod")  # the default is the http loopback
    with pytest.raises(ValidationError, match="https"):
        Settings(environment="prod", public_base_url="http://ownvitals.example")
    assert Settings(environment="prod", public_base_url="https://ownvitals.example")
    for environment in ("dev", "test"):
        assert Settings(environment=environment, public_base_url="http://127.0.0.1:8000")  # pyright: ignore[reportArgumentType]
