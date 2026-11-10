"""Helpers of the lock-order race tests (``test_auth_reset_races``, ``test_auth_lock_order``,
``test_auth_signout_races``).

``AdminPath`` runs the admin Users view's disable and delete as the routes do since #314: one
``admin_session`` on the maint role, its ``ok`` audit row first, then the account definer of
``70-admin-accounts.sql``. The acting admin is a synthetic account made at its first use.
"""

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from authtools import Env
from dbtools import url_for
from flows import Browser
from jobtools import admin_url, engine_for
from ownvitals.admin.audit import Target
from ownvitals.admin.db import Actor, AdminDb, admin_session
from ownvitals.auth import admin_accounts, reset_routes
from ownvitals.auth.tokens import RESET_LIFETIME, issue_token

EMAIL = "user@example.com"
NEW = "a brand new passphrase"
LINKED = "/account?oauth=linked"
WAIT = 0.5


async def reset_token(env: Env, user_id: UUID) -> str:
    async with env.db() as db:
        return await issue_token(db, reset_routes.PURPOSE, user_id, RESET_LIFETIME)


async def complete_reset(env: Env, token: str, remove: list[str] | None = None) -> int:
    response = await Browser(env).post(
        "/api/auth/password/reset/complete",
        {"token": token, "password": NEW, "remove_methods": remove or []},
    )
    return response.status_code


def after(
    monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, hook: Callable[[], Awaitable[None]]
):
    """Run ``hook`` once right after the first call of ``owner.name`` returns."""
    real: Callable[..., Awaitable[Any]] = getattr(owner, name)
    ran: list[bool] = []

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = await real(*args, **kwargs)
        if not ran:
            ran.append(True)
            await hook()
        return result

    monkeypatch.setattr(owner, name, wrapped)


class Held:
    """Patch ``owner.name`` so its first call waits, after the real call, until ``release``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, owner: Any, name: str) -> None:
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        real: Callable[..., Awaitable[Any]] = getattr(owner, name)

        async def held(*args: Any, **kwargs: Any) -> Any:
            result = await real(*args, **kwargs)
            if not self.entered.is_set():
                self.entered.set()
                await asyncio.wait_for(self.release.wait(), 10)
            return result

        monkeypatch.setattr(owner, name, held)

    async def inside(self) -> None:
        await asyncio.wait_for(self.entered.wait(), 10)


async def sessions(env: Env) -> object:
    return await env.scalar("SELECT count(*) FROM sessions")


ADMIN_EMAIL = "synthetic-race-admin@example.com"
Inside = Callable[[], Awaitable[None]] | None


class AdminPath:
    """The admin area's account changes on the real maint path (#314)."""

    def __init__(self, env: Env, db: AdminDb) -> None:
        self.env, self.db = env, db
        self._actor: Actor | None = None

    async def actor(self) -> Actor:
        if self._actor is None:
            admin_id = await self.env.user(ADMIN_EMAIL, role="admin")
            self._actor = Actor(admin_id, ADMIN_EMAIL, str(uuid4()), bytes(32))
        return self._actor

    async def disable(self, user_id: UUID, *, inside: Inside = None) -> tuple[datetime, int]:
        """``POST /users/{id}/disable``'s transaction; ``inside`` runs before it commits."""
        actor = await self.actor()
        target = Target(user_id=user_id)
        async with admin_session(self.db, actor, "user.disable", write=True, target=target) as db:
            result = await admin_accounts.disable_account(db, user_id)
            if inside is not None:
                await inside()
        return result

    async def delete(self, user_id: UUID, *, email: str, inside: Inside = None) -> None:
        """``DELETE /users/{id}``'s transaction; ``inside`` runs before it commits."""
        actor = await self.actor()
        target = Target(user_id=user_id, kind="user", id=str(user_id))
        async with admin_session(self.db, actor, "user.delete", write=True, target=target) as db:
            await admin_accounts.delete_account(db, user_id, email=email, ip=None)
            if inside is not None:
                await inside()


@asynccontextmanager
async def admin_path(env: Env) -> AsyncIterator[AdminPath]:
    """``AdminPath`` on the maint role db-functions installed with the schema, with a password
    for this test (hex only)."""
    password = secrets.token_hex(16)
    with (
        engine_for(admin_url(), isolation_level="AUTOCOMMIT") as engine,
        engine.connect() as connection,
    ):
        connection.execute(text(f"ALTER ROLE ownvitals_maint PASSWORD '{password}'"))
    db = AdminDb(make_url(url_for(admin_url(), "ownvitals_maint", password)))
    try:
        yield AdminPath(env, db)
    finally:
        await db.dispose()
