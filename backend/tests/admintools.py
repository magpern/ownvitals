"""Helpers and fixtures of the admin tests (#12): the schema at head with ``ownvitals_maint``
installed by ``docker/postgres/maint/30-maint.sql``, an app whose admin area runs on it, and
admins with sessions in each state. Every account and address is synthetic.

The maint role exists only while a module that uses ``admin_schema`` runs: the fixture creates it
(as the test superuser, with a random password for this run) and drops it afterwards, so the
other tests never meet it. The role's password and the compose wiring are #126.
"""

import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from authtools import ORIGIN, Env, running_env, session_cookie, truncate_auth
from dbtools import migrated, url_for
from jobtools import admin_url, engine_for, run_script

BACKEND_DIR = Path(__file__).resolve().parent.parent
MAINT_SCRIPT = BACKEND_DIR.parent / "docker" / "postgres" / "maint" / "30-maint.sql"
MAINT_ROLE = "ownvitals_maint"


def drop_maint(url: str) -> None:
    """Remove ``ownvitals_maint`` and every privilege it holds (a leftover too)."""
    with engine_for(url, isolation_level="AUTOCOMMIT") as engine, engine.connect() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": MAINT_ROLE}
        ).first()
        if exists is not None:
            connection.execute(text(f"DROP OWNED BY {MAINT_ROLE} CASCADE"))
            connection.execute(text(f"DROP ROLE {MAINT_ROLE}"))


def install_maint(url: str) -> str:
    """Run 30-maint.sql as the superuser and give the role a random password; its URL."""
    run_script(url, MAINT_SCRIPT.read_text(encoding="utf-8"))
    password = secrets.token_hex(16)
    with engine_for(url, isolation_level="AUTOCOMMIT") as engine, engine.connect() as connection:
        connection.execute(text(f"ALTER ROLE {MAINT_ROLE} PASSWORD '{password}'"))  # hex only
    return url_for(url, MAINT_ROLE, password)


@pytest.fixture(name="admin_schema", scope="module")
def admin_schema_fixture(role_urls: Any) -> Iterator[str]:
    """The schema at head with the maint role installed; yields the maint URL."""
    url = admin_url()
    drop_maint(url)
    with migrated(role_urls.owner):
        try:
            yield install_maint(url)
        finally:
            drop_maint(url)


def truncate_admin(url: str) -> None:
    with engine_for(url, isolation_level="AUTOCOMMIT") as engine, engine.connect() as connection:
        connection.execute(text("TRUNCATE admin_audit, app_settings RESTART IDENTITY"))
    truncate_auth(url)


@dataclass
class AdminEnv:
    env: Env
    maint_url: str

    @property
    def client(self) -> httpx.AsyncClient:
        return self.env.client

    async def audit_rows(self) -> list[Any]:
        """Every audit row, oldest first, as the superuser sees it (``ip_hash`` included)."""
        with engine_for(admin_url()) as engine, engine.connect() as connection:
            return list(connection.execute(text("SELECT * FROM admin_audit ORDER BY id")).all())

    async def superuser(self, sql: str, **params: object) -> Any:
        with engine_for(admin_url()) as engine, engine.begin() as connection:
            return connection.execute(text(sql), params)


@pytest.fixture(name="aenv")
async def aenv_fixture(
    admin_schema: str, app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AdminEnv]:
    """A running app on the app role whose admin area runs as ``ownvitals_maint``."""
    del monkeypatch
    truncate_admin(admin_url())
    password = make_url(admin_schema).password
    async with running_env(
        app_database_url,
        maint_database_url=admin_schema,
        database_maint_password=password,
    ) as running:
        yield AdminEnv(running, admin_schema)


@pytest.fixture(name="plain_env")
async def plain_env_fixture(admin_schema: str, app_database_url: str) -> AsyncIterator[Env]:
    """A running app without maint credentials: the admin area answers 503."""
    del admin_schema
    truncate_admin(admin_url())
    async with running_env(app_database_url) as running:
        yield running


@dataclass
class Caller:
    """One signed-in browser of ``AdminEnv``: its session token and CSRF handling."""

    aenv: AdminEnv
    user_id: UUID
    token: str

    async def csrf(self) -> str:
        info = await self.aenv.client.get("/api/auth/session", headers=session_cookie(self.token))
        return info.json()["csrf_token"]

    async def request(
        self, method: str, path: str, json: Any = None, **kwargs: Any
    ) -> httpx.Response:
        headers = dict(session_cookie(self.token))
        if method not in ("GET", "HEAD"):
            headers.update({"Origin": ORIGIN, "X-CSRF-Token": await self.csrf()})
        headers.update(kwargs.pop("headers", {}))
        response = await self.aenv.client.request(
            method, path, headers=headers, json=json, **kwargs
        )
        self.aenv.client.cookies.clear()
        return response

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", path, **kwargs)

    async def put(self, path: str, json: Any) -> httpx.Response:
        return await self.request("PUT", path, json)

    async def delete(self, path: str) -> httpx.Response:
        return await self.request("DELETE", path)


async def caller(
    aenv: AdminEnv,
    email: str = "admin@example.com",
    *,
    role: str = "admin",
    mfa: bool = True,
    stepup: bool = True,
) -> Caller:
    """A signed-in full session (a confirmed TOTP, so #132's second-factor gate lets it through).

    ``mfa`` keeps the 12-hour admin window; without it that check is older than 12 hours and
    ``admin_gate`` answers ``mfa_required``. ``stepup`` is a fresh step-up (the 5-minute
    admin-write window).
    """
    user_id = await aenv.env.user(email, role=role)
    issued = await aenv.env.full_login(user_id)
    if not mfa:
        await aenv.env.run(
            "UPDATE sessions SET mfa_at = now() - interval '12 hours 1 minute' WHERE id = :i",
            i=issued.id,
        )
    if stepup:
        await aenv.env.run("UPDATE sessions SET stepup_at = now() WHERE id = :i", i=issued.id)
    return Caller(aenv, user_id, issued.token)


async def age_session(aenv: AdminEnv, who: Caller, *, mfa: str | None, stepup: str | None) -> None:
    """Move the session's TOTP or step-up time into the past (a Postgres interval)."""
    if mfa is not None:
        await aenv.env.run(
            "UPDATE sessions SET mfa_at = now() - CAST(:a AS interval) WHERE user_id = :u",
            a=mfa,
            u=who.user_id,
        )
    if stepup is not None:
        await aenv.env.run(
            "UPDATE sessions SET stepup_at = now() - CAST(:a AS interval) WHERE user_id = :u",
            a=stepup,
            u=who.user_id,
        )


def reason(response: httpx.Response) -> str:
    detail = response.json().get("detail")
    return detail.get("reason", "") if isinstance(detail, dict) else ""  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
