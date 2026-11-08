"""Helpers of the lock-order race tests (``test_auth_reset_races``, ``test_auth_lock_order``)."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest

from authtools import Env
from flows import Browser
from ownvitals.auth import reset_routes
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
