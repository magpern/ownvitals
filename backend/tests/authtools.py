"""Helpers for the account and session tests: an app on the app role, users, sessions, cookies."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals import auth
from ownvitals.config import Settings
from ownvitals.db import system_session
from ownvitals.web import create_app

ORIGIN = "https://ownvitals.test"
APP_SECRET = "test-app-secret-key-0123456789abcdef0123456789abcdef"
ADMIN_EMAIL = "Owner@Example.com"
# A fixed, public test KEK (ADR 0004 section 2), built at run time; never a real key. The autouse
# ``kek_dir`` fixture (conftest) writes it as ``connection_kek``.
TEST_KEK = bytes(range(32))


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
            return await auth.create_user(db, email, **kwargs)  # pyright: ignore[reportArgumentType]

    async def login(
        self, user_id: UUID, *, mfa: bool = False, ip: str = "203.0.113.5"
    ) -> auth.IssuedSession:
        async with self.db() as db:
            return await auth.start_session(
                db, self.settings, user_id, user_agent="pytest", ip=ip, mfa=mfa
            )

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
    return {"Cookie": f"{auth.SESSION_COOKIE}={token}"}


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
