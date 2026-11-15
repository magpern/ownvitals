"""The secret files of ``ownvitals.config``: where they live, which fields they set, and how
they are read."""

import os
from pathlib import Path

DEFAULT_SECRETS_DIR = "/run/secrets"
# Fields read from secret files, and never from the environment.
SECRET_FILE_FIELDS = (
    "database_password",
    "database_owner_password",
    "database_maint_password",
    "app_secret_key",
    "smtp_password",
    "oauth_google_client_secret",
    "turnstile_secret_key",
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
