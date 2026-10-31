"""Application settings, read from the environment (prefix ``OWNVITALS_``) and secret files.

This module holds every setting the platform milestone (M0) needs, so the issues that build on
it (database roles, mail, encryption, jobs, accounts, admin) add no field here. A setting whose
module is not written yet is documented where it is declared.
"""

import ipaddress
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from ownvitals.emailnorm import normalize_email

DATABASE_DRIVER = "postgresql+psycopg"
DEFAULT_SECRETS_DIR = "/run/secrets"
# Fields read from secret files, and never from the environment.
SECRET_FILE_FIELDS = (
    "database_password",
    "database_owner_password",
    "database_maint_password",
    "app_secret_key",
    "smtp_password",
    "oauth_google_client_secret",
)
# The only libpq parameters a database URL may carry in its query string in prod (an allowlist,
# compared exactly): verifying the server and labelling the connection. Everything else is
# refused, among them ``password``, ``passfile`` and ``sslpassword`` (a password from somewhere
# other than the secret file), ``conninfo`` and ``dsn`` (a whole second connection string,
# password included), ``service`` (a service file picks host, user and password), ``options``
# (``-c`` sets session settings, ``app.user_id`` among them) and ``host``/``hostaddr`` (the host
# is the URL's own).
PROD_QUERY_PARAMETERS = frozenset(
    {
        "application_name",
        "channel_binding",
        "require_auth",
        "ssl_min_protocol_version",
        "sslcrl",
        "sslcrldir",
        "sslmode",
        "sslrootcert",
        "sslsni",
    }
)
# Hosts where unencrypted SMTP (``smtp_tls = none``) is allowed: loopback and the compose service.
PLAIN_SMTP_HOSTS = frozenset({"localhost", "mailpit"})
MIN_APP_SECRET_BYTES = 32
# The id of the current KEK (the ``connection_kek`` secret file, read by the crypto package).
CONNECTION_KEK_ID = re.compile(r"kek-[a-z0-9-]{1,28}")


def secrets_dir() -> Path:
    """Where secret files live: ``OWNVITALS_SECRETS_DIR``, default ``/run/secrets``."""
    return Path(os.environ.get("OWNVITALS_SECRETS_DIR") or DEFAULT_SECRETS_DIR)


def read_secret_files(directory: Path, names: tuple[str, ...]) -> dict[str, str]:
    """Read ``<directory>/<name>`` for each name; missing files and directories are skipped."""
    if not directory.is_dir():
        return {}
    values: dict[str, str] = {}
    for name in names:
        path = directory / name
        if path.is_file():
            text = path.read_bytes().decode("utf-8")
            # Only the trailing newline an editor or `echo` adds; spaces may be part of a secret.
            values[name] = text.removesuffix("\n").removesuffix("\r")
    return values


class SecretFilesSettingsSource(PydanticBaseSettingsSource):
    """Secrets come only from files, named after the field without the env prefix.

    ``/run/secrets/database_password`` sets ``database_password``. pydantic-settings' own
    secrets source would look for ``ownvitals_database_password`` because of ``env_prefix``.
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False  # unused: __call__ reads the files directly

    def __call__(self) -> dict[str, Any]:
        return dict(read_secret_files(secrets_dir(), SECRET_FILE_FIELDS))


class WithoutSecretsSource(PydanticBaseSettingsSource):
    """Wraps the environment source and drops the secret fields: env vars never set secrets."""

    def __init__(self, inner: PydanticBaseSettingsSource) -> None:
        super().__init__(inner.settings_cls)
        self._inner = inner

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._inner.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        return {k: v for k, v in self._inner().items() if k not in SECRET_FILE_FIELDS}


class Settings(BaseSettings):
    """All configuration of the web app and the worker.

    Everything that can carry a credential is a ``SecretStr``: it is masked in ``repr``,
    ``str``, ``model_dump`` and validation errors.
    """

    model_config = SettingsConfigDict(
        env_prefix="OWNVITALS_",
        env_ignore_empty=True,
        hide_input_in_errors=True,
        extra="ignore",
    )

    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Database. May embed a password, so each URL is a secret too.
    # The app role (``ownvitals_app``): web and worker.
    database_url: SecretStr = SecretStr(
        f"{DATABASE_DRIVER}://ownvitals_app@127.0.0.1:5432/ownvitals"
    )
    # From the file ``database_password`` in the secrets dir (never the environment).
    # When set, replaces the password in ``database_url``.
    database_password: SecretStr | None = None
    # The owner role (``ownvitals_owner``): Alembic only, never web or worker.
    migrations_database_url: SecretStr = SecretStr(
        f"{DATABASE_DRIVER}://ownvitals_owner@127.0.0.1:5432/ownvitals"
    )
    # From the file ``database_owner_password``; replaces the password in the URL above.
    database_owner_password: SecretStr | None = None
    # The admin role (``ownvitals_maint``): web only, never the worker. The admin area works only
    # with both the URL and the ``database_maint_password`` file; without either its routes
    # answer 503 ``admin_unavailable``.
    maint_database_url: SecretStr | None = None
    database_maint_password: SecretStr | None = None
    # The built single-page app (the image sets it); unset: the API only.
    static_dir: Path | None = None

    # Accounts and sessions.
    # From the file ``app_secret_key`` (at least 32 bytes); the web app refuses to start without
    # it once accounts exist. All auth keys are derived from it.
    app_secret_key: SecretStr | None = None
    # The one public origin, for the Origin check and for links in mail.
    public_base_url: str = "http://127.0.0.1:8000"
    # Comma-separated addresses that become admin at their next sign-in.
    admin_emails: str = ""
    # Comma-separated addresses or CIDR ranges of reverse proxies whose X-Forwarded-For is
    # believed. Empty: the peer address is the client.
    trusted_proxies: str = ""
    session_idle_days: int = 7
    session_absolute_days: int = 30
    password_hash_workers: int = 4
    # Who may create an account: ``invite`` (default), ``closed`` or ``open``.
    signup_mode: Literal["invite", "closed", "open"] = "invite"
    # How long a fresh TOTP check authorises a sensitive action (step-up, PLAN §6: within 10
    # minutes). The environment can shorten the window, never lengthen it.
    stepup_window_minutes: int = Field(default=10, ge=1, le=10)
    # How often the worker purges expired auth rows.
    purge_interval_seconds: float = 3600.0

    # The id of the current key-encryption key. The key itself is the secret file
    # ``connection_kek`` (read by the crypto package, never the environment); retired keys are
    # ``connection_kek_<id>``.
    connection_kek_id: str = "kek-1"

    # Mail. Unset ``smtp_host``: no mail is sent (a warning is logged instead).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_sender: str = "ownvitals@localhost"
    smtp_tls: Literal["starttls", "tls", "none"] = "starttls"
    smtp_username: str | None = None
    # From the file ``smtp_password`` (never the environment).
    smtp_password: SecretStr | None = None

    # Google sign-in, the only OAuth provider. It is on only when the client id and the secret
    # file ``oauth_google_client_secret`` (never the environment) both exist.
    oauth_google_client_id: str | None = None
    oauth_google_client_secret: SecretStr | None = None

    # Jobs and the worker.
    # The worker's claim poll when no ``jobs_queued`` notification arrives.
    worker_poll_seconds: float = 2.0
    job_lease_seconds: int = Field(default=120, ge=30, le=600)
    # The worker's engine pool follows it.
    worker_concurrency: int = Field(default=2, ge=1, le=16)
    # Reap, expire and settle every this many seconds.
    worker_maintenance_seconds: float = Field(default=30.0, gt=0)
    # On SIGTERM, how long runs may take to reach a checkpoint or return from a call before their
    # leases are handed back (inside compose's 30 s stop_grace_period).
    worker_shutdown_wait_seconds: float = Field(default=25.0, ge=0, le=25)
    # Queued and running jobs per user; creation above it is a 429.
    jobs_per_user: int = Field(default=2, ge=1, le=100)
    # ``operation_busy`` requeues per phase that cost no attempt; after that each one counts.
    job_busy_requeue_limit: int = Field(default=5, ge=0, le=100)

    # The environment side of the stored feature flags (the stricter of the two wins): both are
    # off by default and a stored flag can only keep them off. ``ai_insights`` also gates the
    # model host on the worker's egress allowlist.
    experimental_findings: bool = False
    ai_insights: bool = False

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            WithoutSecretsSource(env_settings),
            SecretFilesSettingsSource(settings_cls),
        )

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("database_url", "migrations_database_url", "maint_database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr | None, info: ValidationInfo) -> SecretStr | None:
        if value is None:
            return None
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError(f"{info.field_name} is not a valid database URL") from None
        if url.drivername != DATABASE_DRIVER:
            raise ValueError(f"{info.field_name} must use the {DATABASE_DRIVER}:// scheme")
        return value

    @field_validator("app_secret_key")
    @classmethod
    def _check_app_secret_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value().encode()) < MIN_APP_SECRET_BYTES:
            raise ValueError(f"app_secret_key must be at least {MIN_APP_SECRET_BYTES} bytes long")
        return value

    @field_validator("connection_kek_id")
    @classmethod
    def _check_connection_kek_id(cls, value: str) -> str:
        if CONNECTION_KEK_ID.fullmatch(value) is None:
            raise ValueError("connection_kek_id must match ^kek-[a-z0-9-]{1,28}$")
        return value

    @field_validator("public_base_url")
    @classmethod
    def _check_public_base_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
        ):
            raise ValueError("public_base_url must be an http(s) origin without a path")
        return public_origin(value)

    @model_validator(mode="after")
    def _prod_needs_https(self) -> "Settings":
        if self.environment == "prod" and not self.public_base_url.startswith("https://"):
            raise ValueError("public_base_url must be https:// when environment is prod")
        return self

    @model_validator(mode="after")
    def _prod_takes_passwords_only_from_files(self) -> "Settings":
        # The image defaults to prod, so a stack that forgets to say otherwise fails closed. In
        # prod a password embedded in a database URL (an environment variable, visible in
        # ``docker inspect`` and process listings) is refused: the passwords come from the
        # secret files, and the URLs carry the user only. The query string may hold only the
        # parameters of ``PROD_QUERY_PARAMETERS``. The message names the field, never the URL
        # or the parameter (a mistyped key could be a password).
        if self.environment == "prod":
            for name in ("database_url", "migrations_database_url", "maint_database_url"):
                url: SecretStr | None = getattr(self, name)
                if url is None:
                    continue
                parsed = make_url(url.get_secret_value())
                if parsed.password is not None:
                    raise ValueError(
                        f"{name} must not embed a password when environment is prod; "
                        "the password comes from its secret file"
                    )
                if not set(parsed.query) <= PROD_QUERY_PARAMETERS:
                    raise ValueError(
                        f"{name} may carry only these query parameters when environment is "
                        f"prod: {', '.join(sorted(PROD_QUERY_PARAMETERS))}; the password comes "
                        "from its secret file"
                    )
        return self

    @model_validator(mode="after")
    def _plain_smtp_only_locally(self) -> "Settings":
        if (
            self.smtp_tls == "none"
            and self.smtp_host is not None
            and not _is_local_host(self.smtp_host)
        ):
            raise ValueError(
                "smtp_tls=none is only allowed for a loopback host or the mailpit service"
            )
        return self

    @field_validator("smtp_port")
    @classmethod
    def _check_smtp_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("smtp_port must be between 1 and 65535")
        return value

    @field_validator("trusted_proxies")
    @classmethod
    def _check_trusted_proxies(cls, value: str) -> str:
        parse_networks(value)
        return value

    @field_validator("session_idle_days", "session_absolute_days", "password_hash_workers")
    @classmethod
    def _check_positive(cls, value: int, info: ValidationInfo) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be at least 1")
        return value

    @field_validator("worker_poll_seconds", "purge_interval_seconds")
    @classmethod
    def _check_interval(cls, value: float, info: ValidationInfo) -> float:
        if value <= 0:
            raise ValueError(f"{info.field_name} must be greater than 0")
        return value

    @property
    def admin_email_set(self) -> frozenset[str]:
        """``admin_emails`` in the normalized form (ASCII lower case)."""
        return frozenset(normalize_email(e) for e in self.admin_emails.split(",") if e.strip())

    @property
    def trusted_proxy_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return parse_networks(self.trusted_proxies)

    def sqlalchemy_url(self) -> URL:
        """The app role's URL, with ``database_password`` substituted when it is set."""
        return _with_password(self.database_url, self.database_password)

    def migrations_sqlalchemy_url(self) -> URL:
        """The owner role's URL, with ``database_owner_password`` substituted when it is set."""
        return _with_password(self.migrations_database_url, self.database_owner_password)

    def maint_sqlalchemy_url(self) -> URL | None:
        """The admin role's URL with its password, or ``None`` when either is missing (an empty
        password file counts as none). Only the admin package may call it."""
        password = self.database_maint_password
        if self.maint_database_url is None or password is None or not password.get_secret_value():
            return None
        return _with_password(self.maint_database_url, password)


def _is_local_host(host: str) -> bool:
    if host.lower() in PLAIN_SMTP_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def public_origin(url: str) -> str:
    """``scheme://host[:port]`` of ``url``, lower case, as a browser sends it in ``Origin``."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    default = {"http": 80, "https": 443}.get(parts.scheme.lower())
    port = f":{parts.port}" if parts.port is not None and parts.port != default else ""
    return f"{parts.scheme.lower()}://{host.lower()}{port}"


def parse_networks(
    value: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse a comma-separated list of addresses and CIDR ranges."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in value.split(","):
        if item.strip():
            try:
                networks.append(ipaddress.ip_network(item.strip(), strict=False))
            except ValueError:
                raise ValueError("trusted_proxies must list IP addresses or CIDR ranges") from None
    return tuple(networks)


def _with_password(url: SecretStr, password: SecretStr | None) -> URL:
    result = make_url(url.get_secret_value())
    if password is not None:
        result = result.set(password=password.get_secret_value())
    return result
