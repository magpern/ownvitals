"""The secret files of ``ownvitals.config``: where they live, which fields they set, and how
they are read, and the two settings sources that keep secrets out of the environment."""

import os
from pathlib import Path
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import PydanticBaseSettingsSource

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
