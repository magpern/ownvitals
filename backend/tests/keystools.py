"""Helpers for the key-ring tests: key files, settings, engines and the check table.

#13 adds the ``keys rewrap`` and ``keys wipe`` helpers here. Every key is synthetic test
material, built from ``bytes(range(...))`` or random at test time; none is a real key.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.config import Settings
from ownvitals.db import create_engine as create_app_engine
from ownvitals.db import create_session_factory

KEY_A = bytes(range(32))
KEY_B = bytes(range(64, 96))
KEY_C = bytes(range(128, 160))
KEY_D = bytes(range(192, 224))


def key_file(key: bytes, *, newline: bool = True) -> bytes:
    """A key file's content: 64 lowercase hex characters and, by default, one newline."""
    return key.hex().encode() + (b"\n" if newline else b"")


def write_key(directory: Path, name: str, key: bytes) -> None:
    (directory / name).write_bytes(key_file(key))


def rotate(directory: Path, new_id: str, new_key: bytes, old_id: str) -> None:
    """The rotation runbook (ADR 0004 section 3): the old current file becomes
    ``connection_kek_<old id>`` and the new key is ``connection_kek``."""
    (directory / "connection_kek").rename(directory / f"connection_kek_{old_id}")
    write_key(directory, "connection_kek", new_key)


def settings_for(app_url: str, **overrides: Any) -> Settings:
    """Settings of web or the worker on the app role's URL."""
    values: dict[str, Any] = {"environment": "test", "database_url": app_url, **overrides}
    return Settings(**values)


@asynccontextmanager
async def app_factory(settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory on an engine as web and the worker build it; disposed afterwards."""
    engine = create_app_engine(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@contextmanager
def superuser(database_url: str) -> Iterator[Connection]:
    """One committed transaction as the test database's superuser (setup and probes only)."""
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()


def check_rows(database_url: str) -> dict[str, bytes]:
    """``key_id -> check_value`` of every row of ``connection_keks``."""
    with superuser(database_url) as connection:
        rows = connection.execute(text("SELECT key_id, check_value FROM connection_keks"))
        return {row.key_id: bytes(row.check_value) for row in rows}


def clear_check_rows(database_url: str) -> None:
    with superuser(database_url) as connection:
        connection.execute(text("DELETE FROM connection_keks"))


def insert_check_row(database_url: str, key_id: str, value: bytes) -> None:
    with superuser(database_url) as connection:
        connection.execute(
            text("INSERT INTO connection_keks (key_id, check_value) VALUES (:k, :v)"),
            {"k": key_id, "v": value},
        )
