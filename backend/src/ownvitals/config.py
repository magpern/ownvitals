"""Application settings, read from the environment (prefix ``OWNVITALS_``) and secret files.

This module holds every setting the platform milestone (M0) needs, so the issues that build on
it (database roles, mail, encryption, jobs, accounts, admin) add no field here. A setting whose
module is not written yet is documented where it is declared.
"""

import ipaddress
import math
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, ValidationInfo, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

# The secret files and the host and network helpers live in their own modules; re-exported here.
from ownvitals.config_ai import AiSettings
from ownvitals.config_auth import AuthSettings
from ownvitals.config_jobs import JobSettings
from ownvitals.config_mail import DEFAULT_SMTP_TOTAL_SECONDS, MailSettings  # noqa: F401
from ownvitals.config_net import (  # noqa: F401 (re-exported)
    PLAIN_SMTP_HOSTS,
    _is_ip,
    _is_local_host,
    parse_networks,
    public_origin,
)
from ownvitals.config_secrets import (  # noqa: F401 (re-exported)
    DEFAULT_SECRETS_DIR,
    SECRET_FILE_FIELDS,
    read_secret_files,
    secrets_dir,
)
from ownvitals.config_webhooks import WebhookSettings
from ownvitals.emailnorm import normalize_email

DATABASE_DRIVER = "postgresql+psycopg"
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
MIN_APP_SECRET_BYTES = 32
# The id of the current KEK (the ``connection_kek`` secret file, read by the crypto package).
CONNECTION_KEK_ID = re.compile(r"kek-[a-z0-9-]{1,28}")
# The ids of the current webhook-secret seed and pepper (the ``webhook_seed`` and
# ``webhook_pepper`` secret files, read by the crypto package; ADR 0004 section 4).
WEBHOOK_SEED_ID = re.compile(r"seed-[a-z0-9-]{1,28}")
WEBHOOK_PEPPER_ID = re.compile(r"pep-[a-z0-9-]{1,28}")


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


class Settings(AuthSettings, MailSettings, JobSettings, AiSettings, WebhookSettings, BaseSettings):
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
    # ADR 0011 open question 1. Refused true until the owner answers it: a passkey replaces the
    # password only, and every sign-in still completes TOTP. No code path reads it as true.
    passkey_satisfies_second_factor: bool = False
    # The id of the current key-encryption key. The key itself is the secret file
    # ``connection_kek`` (read by the crypto package, never the environment); retired keys are
    # ``connection_kek_<id>``.
    connection_kek_id: str = "kek-1"

    # From the file ``smtp_password`` (never the environment).
    smtp_password: SecretStr | None = None

    # Google sign-in, the only OAuth provider. It is on only when the client id and the secret
    # file ``oauth_google_client_secret`` (never the environment) both exist.
    oauth_google_client_secret: SecretStr | None = None

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

    @field_validator("webhook_seed_id")
    @classmethod
    def _check_webhook_seed_id(cls, value: str) -> str:
        if WEBHOOK_SEED_ID.fullmatch(value) is None:
            raise ValueError("webhook_seed_id must match ^seed-[a-z0-9-]{1,28}$")
        return value

    @field_validator("webhook_pepper_id")
    @classmethod
    def _check_webhook_pepper_id(cls, value: str) -> str:
        if WEBHOOK_PEPPER_ID.fullmatch(value) is None:
            raise ValueError("webhook_pepper_id must match ^pep-[a-z0-9-]{1,28}$")
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

    @field_validator("passkey_satisfies_second_factor")
    @classmethod
    def _refuse_passkey_second_factor(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "passkey_satisfies_second_factor=true is refused until the owner decides "
                "(ADR 0011 open question 1)"
            )
        return value

    @field_validator("geo_source")
    @classmethod
    def _refuse_geoip_file(cls, value: str) -> str:
        if value == "geoip_file":
            raise ValueError(
                "geo_source=geoip_file is refused until the owner decides (ADR 0011 open "
                "question 2) and a reviewed PR adds the reader"
            )
        return value

    @model_validator(mode="after")
    def _check_webauthn_rp_id(self) -> "Settings":
        host = urlsplit(self.public_base_url).hostname or ""
        rp_id = self.webauthn_rp_id
        if rp_id is None:
            return self
        if _is_ip(host) or rp_id != rp_id.strip().lower() or not rp_id or _is_ip(rp_id):
            raise ValueError("webauthn_rp_id must be a lower-case domain name")
        if not (host == rp_id or host.endswith("." + rp_id)) or (
            "." not in rp_id and rp_id != "localhost"
        ):
            raise ValueError(
                "webauthn_rp_id must be the host of public_base_url or a parent domain of it"
            )
        return self

    @model_validator(mode="after")
    def _ai_user_cap_within_global(self) -> "Settings":
        # ADR 0014 section 7: settings refuse a per-user cap above the global one.
        if self.ai_user_monthly_cap_microusd > self.ai_global_monthly_cap_microusd:
            raise ValueError("ai_user_monthly_cap_microusd must not exceed the global cap")
        return self

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

    @field_validator("trusted_proxies", "cf_connecting_ip_proxies")
    @classmethod
    def _check_trusted_proxies(cls, value: str, info: ValidationInfo) -> str:
        parse_networks(value, info.field_name or "trusted_proxies")
        return value

    @field_validator("signup_mode", mode="before")
    @classmethod
    def _refuse_open_signup(cls, value: object) -> object:
        if value == "open":
            raise ValueError("signup_mode=open is refused until the M8 gate (ADR 0003 section 6)")
        return value

    @field_validator("smtp_total_seconds")
    @classmethod
    def _check_smtp_total_seconds(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("smtp_total_seconds must be positive and finite")
        return value

    @field_validator("session_idle_hours", "session_absolute_days", "password_hash_workers")
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
    def passkey_rp_id(self) -> str | None:
        """The WebAuthn relying-party id (ADR 0011 section 1), or ``None`` when the public host
        is an IP address and passkeys are therefore off."""
        if self.webauthn_rp_id is not None:
            return self.webauthn_rp_id
        host = urlsplit(self.public_base_url).hostname or ""
        return None if not host or _is_ip(host) else host

    @property
    def admin_email_set(self) -> frozenset[str]:
        """``admin_emails`` in the normalized form (ASCII lower case)."""
        return frozenset(normalize_email(e) for e in self.admin_emails.split(",") if e.strip())

    @property
    def trusted_proxy_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return parse_networks(self.trusted_proxies)

    @property
    def cf_connecting_ip_networks(
        self,
    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return parse_networks(self.cf_connecting_ip_proxies, "cf_connecting_ip_proxies")

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


def _with_password(url: SecretStr, password: SecretStr | None) -> URL:
    result = make_url(url.get_secret_value())
    if password is not None:
        result = result.set(password=password.get_secret_value())
    return result
