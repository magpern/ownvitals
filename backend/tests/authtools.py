"""Helpers and fixtures of the account and session tests: an app on the app role, users,
sessions, cookies.

The fixtures (``auth_schema``, ``clean_auth``, ``env``, ``smtp``, ``menv``) live here, not in
``conftest.py``: a test module imports the ones it uses (``# noqa: F401 - fixtures``), as the jobs
tests do with ``jobapi``. Every account and address is synthetic.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, func, insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.accounts import create_user
from ownvitals.auth.cookies import SESSION_COOKIE
from ownvitals.auth.sessions import IssuedSession, start_session
from ownvitals.auth.tables import TABLE_NAMES, mfa_totp
from ownvitals.auth.totp_keys import seal_totp_secret
from ownvitals.config import Settings
from ownvitals.db import system_session
from ownvitals.web import create_app
from smtpfake import SmtpFake

ORIGIN = "https://ownvitals.test"
APP_SECRET = "test-app-secret-key-0123456789abcdef0123456789abcdef"
ADMIN_EMAIL = "Owner@Example.com"
# A synthetic TOTP secret (160 bits, base32) for ``Env.enrol``; never a real authenticator's.
TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def make_settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "database_url": database_url,
        "app_secret_key": APP_SECRET,
        "public_base_url": ORIGIN,
        **overrides,
    }
    return Settings(**values)  # pyright: ignore[reportArgumentType]


@dataclass
class Env:
    app: FastAPI
    client: httpx.AsyncClient
    settings: Settings

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        return self.app.state.session_factory

    def db(self):
        return system_session(self.factory)

    async def user(self, email: str = "user@example.com", **kwargs: object) -> UUID:
        async with self.db() as db:
            return await create_user(db, email, **kwargs)  # pyright: ignore[reportArgumentType]

    async def login(
        self, user_id: UUID, *, mfa: bool = False, ip: str = "203.0.113.5"
    ) -> IssuedSession:
        async with self.db() as db:
            return await start_session(
                db, self.settings, user_id, user_agent="pytest", ip=ip, mfa=mfa
            )

    async def enrol(self, user_id: UUID, secret: str = TEST_TOTP_SECRET) -> str:
        """Give the user a confirmed TOTP (envelope-sealed, as an enrolment stores it) without
        going through the API. Returns the secret."""
        async with self.db() as db:
            values = await seal_totp_secret(db, self.app.state.keyring, user_id, secret.encode())
            await db.execute(
                insert(mfa_totp).values(
                    user_id=user_id, confirmed_at=func.now(), last_step=0, **values
                )
            )
        return secret

    async def full_login(
        self, user_id: UUID, *, ip: str = "203.0.113.5", stepup: bool = False
    ) -> IssuedSession:
        """A full session (ownvitals ADR 0003 section 2): the user gets a confirmed TOTP if it
        has none, and the session counts as having passed it (``stepup``: just now as well)."""
        if not await self.scalar("SELECT count(*) FROM mfa_totp WHERE user_id = :u", u=user_id):
            await self.enrol(user_id)
        issued = await self.login(user_id, mfa=True, ip=ip)
        if stepup:
            await self.run("UPDATE sessions SET stepup_at = now() WHERE id = :i", i=issued.id)
        return issued

    async def scalar(self, sql: str, **params: object) -> object:
        async with self.db() as db:
            return (await db.execute(text(sql), params)).scalar_one_or_none()

    async def row(self, sql: str, **params: object) -> Any:
        async with self.db() as db:
            return (await db.execute(text(sql), params)).one()

    async def run(self, sql: str, **params: object) -> None:
        async with self.db() as db:
            await db.execute(text(sql), params)


def session_cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE}={token}"}


def unsafe_headers(token: str | None = None, csrf: str | None = None) -> dict[str, str]:
    """Headers of a same-origin state-changing request."""
    headers = {"Origin": ORIGIN}
    if token:
        headers.update(session_cookie(token))
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers


def set_cookies(response: httpx.Response) -> list[str]:
    return response.headers.get_list("set-cookie")


@asynccontextmanager
async def running_env(database_url: str, **overrides: object) -> AsyncIterator[Env]:
    settings = make_settings(database_url, **overrides)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            yield Env(app, client, settings)


def truncate_auth(admin_url: str) -> None:
    """Empty every account table (and so, by cascade, every job of a user), as the superuser."""
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text(f"TRUNCATE {', '.join(TABLE_NAMES)} CASCADE"))
    finally:
        engine.dispose()


@pytest.fixture(name="auth_schema", scope="module")
def auth_schema_fixture(role_urls: Any) -> Iterator[None]:
    """The migrated schema (every revision) for the tests of one module."""
    from dbtools import migrated

    with migrated(role_urls.owner):
        yield


@pytest.fixture(name="clean_auth")
def clean_auth_fixture(auth_schema: None, database_url: str) -> None:
    """The migrated schema with every account table empty."""
    truncate_auth(database_url)


@pytest.fixture(name="env")
async def env_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    """A running app on the app role over an empty, migrated database."""
    async with running_env(app_database_url) as running:
        yield running


@pytest.fixture(name="smtp")
async def smtp_fixture() -> AsyncIterator[SmtpFake]:
    """An in-process SMTP server on loopback that keeps every message."""
    async with SmtpFake() as fake:
        yield fake


@pytest.fixture(name="menv")
async def menv_fixture(
    clean_auth: None, app_database_url: str, smtp: SmtpFake
) -> AsyncIterator[Env]:
    """A running app whose mail goes to the SMTP fake."""
    async with running_env(
        app_database_url, smtp_host="127.0.0.1", smtp_port=smtp.port, smtp_tls="none"
    ) as running:
        yield running
