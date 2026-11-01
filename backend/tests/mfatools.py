"""Helpers for the MFA tests: an actor that holds a session token, a fake notifier, a clock."""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import pytest

from authtools import (  # noqa: F401 - fixtures
    ORIGIN,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    session_cookie,
)
from ownvitals.auth import totp
from ownvitals.auth.cookies import SESSION_COOKIE
from ownvitals.auth.notices import register_security_notifier

T0 = 1_700_000_010  # a fixed instant; step = T0 // 30


class FakeNotifier:
    def __init__(self) -> None:
        self.enrolled: list[UUID] = []
        self.failures: list[UUID] = []

    async def totp_enrolled(self, user_id: UUID, email: str) -> None:
        self.enrolled.append(user_id)

    async def mfa_failures(self, user_id: UUID, email: str) -> None:
        self.failures.append(user_id)


@pytest.fixture(name="notifier")
def notifier_fixture() -> Iterator[FakeNotifier]:
    fake = FakeNotifier()
    register_security_notifier(fake)
    yield fake
    register_security_notifier(None)


@pytest.fixture(name="clock")
def clock_fixture(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The TOTP clock; ``clock[0] = ...`` moves it."""
    now = [float(T0)]
    monkeypatch.setattr(totp, "clock", lambda: now[0])
    return now


def step_of(now: float) -> int:
    return int(now // totp.PERIOD)


@dataclass
class Actor:
    """One browser: its own cookie jar, the session token it holds, and CSRF handling."""

    env: Env
    token: str
    client: httpx.AsyncClient

    async def call(self, method: str, path: str, json: Any = None) -> httpx.Response:
        info = await self.client.get("/api/auth/session", headers=session_cookie(self.token))
        headers = {
            "Origin": ORIGIN,
            "X-CSRF-Token": info.json()["csrf_token"],
            **session_cookie(self.token),
        }
        response = await self.client.request(method, path, headers=headers, json=json)
        self.client.cookies.clear()
        for cookie in response.headers.get_list("set-cookie"):
            if cookie.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" not in cookie:
                self.token = cookie.split(";")[0].split("=", 1)[1]
        return response

    async def get(self, path: str) -> httpx.Response:
        response = await self.client.get(path, headers=session_cookie(self.token))
        self.client.cookies.clear()
        return response

    async def post(self, path: str, json: Any = None) -> httpx.Response:
        return await self.call("POST", path, json)

    async def enroll(self) -> tuple[str, list[str]]:
        """Enroll TOTP with the current test clock; returns (secret, recovery codes)."""
        start = await self.post("/api/auth/mfa/totp/start")
        assert start.status_code == 200, start.text
        body = start.json()
        code = totp.code_for_step(body["secret"], step_of(totp.clock()))
        done = await self.post(
            "/api/auth/mfa/totp/confirm", {"pending": body["pending"], "code": code}
        )
        assert done.status_code == 200, done.text
        return body["secret"], done.json()["recovery_codes"]


@pytest.fixture(name="make_actor")
async def make_actor_fixture(env: Env) -> AsyncIterator[Any]:
    clients: list[httpx.AsyncClient] = []

    async def make(user_id: UUID, *, mfa: bool = False) -> Actor:
        """``mfa``: a full session (#11: the user gets a TOTP through ``Env.enrol`` if it has
        none); without it, a session that has not passed its second factor."""
        issued = await (env.full_login(user_id) if mfa else env.login(user_id))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url=ORIGIN)
        clients.append(client)
        return Actor(env, issued.token, client)

    yield make
    for client in clients:
        await client.aclose()


async def admin_with_totp(
    env: Env, make_actor: Any, email: str = "admin@example.com"
) -> tuple[Actor, str, list[str], UUID]:
    """An admin with TOTP enrolled: (actor, secret, recovery codes, user id)."""
    user_id = await env.user(email, role="admin")
    actor = await make_actor(user_id)
    secret, codes = await actor.enroll()
    return actor, secret, codes, user_id
