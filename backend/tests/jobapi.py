"""Helpers for the jobs API tests (ADR 0005 sections 2 and 7): the real app (``create_app``, its
lifespan and the jobs router's) on the app role, with two test-only overrides:

- ``current_user`` (fail closed until #10) is replaced by a test-only issuer that reads the user
  id from a test header and builds the context with ``ctx.make_context``;
- ``job_kinds`` (``KINDS`` is empty in #9) by a registry with the test-only kind.

Synthetic users (random UUIDs) and inputs only.
"""

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Header

from ctx import make_context, same_origin_client
from jobcore import JobsEnv, registry_for, settings_for
from jobtools import KIND, ensure_user
from ownvitals.context import UserContext
from ownvitals.jobs import Complete, JobRun, KindRegistry
from ownvitals.jobs.deps import current_user
from ownvitals.jobs.routes import job_kinds
from ownvitals.web import create_app

UNKNOWN = "00000000-0000-4000-8000-000000000000"
USER_HEADER = "X-Test-User"


def header_user(x_test_user: Annotated[str, Header()]) -> UserContext:
    """The test-only stand-in for ``ownvitals.auth``'s issuer (#10)."""
    return make_context(UUID(x_test_user))


async def _complete(job_run: JobRun) -> Complete:
    return Complete()


def api_registry() -> KindRegistry:
    return registry_for(_complete)


def new_key() -> str:
    return secrets.token_urlsafe(18)


@dataclass
class Browser:
    """One signed-in user's client."""

    client: httpx.AsyncClient
    user_id: UUID

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {USER_HEADER: str(self.user_id)} | (extra or {})

    async def post(
        self, path: str, json: Any = None, extra: dict[str, str] | None = None
    ) -> httpx.Response:
        return await self.client.post(path, headers=self.headers(extra), json=json)

    async def get(self, path: str) -> httpx.Response:
        return await self.client.get(path, headers=self.headers())

    @staticmethod
    def body(**input_: Any) -> dict[str, Any]:
        return {"kind": KIND, "input": input_}

    async def create(self, key: str | None = None, **input_: Any) -> httpx.Response:
        return await self.post(
            "/api/jobs", self.body(**input_), {"Idempotency-Key": key or new_key()}
        )


@dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient
    env: JobsEnv

    def browser(self, user_id: UUID | None = None) -> Browser:
        """A signed-in user's client; the user's synthetic account exists (``jobs.owner_id``
        references ``users`` since #10)."""
        user_id = user_id or uuid4()
        ensure_user(self.env.admin, user_id)
        return Browser(self.client, user_id)


@pytest.fixture(name="api")
async def api_fixture(jenv: JobsEnv, app_database_url: str) -> AsyncIterator[Api]:
    """The app with its lifespans running and the two test overrides."""
    app = create_app(settings_for(app_database_url))
    app.dependency_overrides[current_user] = header_user
    app.dependency_overrides[job_kinds] = api_registry
    async with app.router.lifespan_context(app):  # pyright: ignore[reportArgumentType]
        transport = httpx.ASGITransport(app=app)
        async with same_origin_client(transport, app) as client:
            yield Api(app, client, jenv)


@pytest.fixture(name="bare_app")
async def bare_app_fixture(
    jenv: JobsEnv, app_database_url: str
) -> AsyncIterator[httpx.AsyncClient]:
    """The app as shipped: no override, so the fail-closed defaults answer."""
    app = create_app(settings_for(app_database_url))
    async with app.router.lifespan_context(app):  # pyright: ignore[reportArgumentType]
        transport = httpx.ASGITransport(app=app)
        async with same_origin_client(transport, app) as client:
            yield client


def plant_running(env: JobsEnv, owner: UUID) -> UUID:
    """A running job of ``owner`` (it counts towards the per-user limit)."""
    lease = datetime.now(UTC) + timedelta(minutes=5)
    _, job_id = env.job(
        owner,
        state="running",
        attempt=1,
        lease_version=1,
        next_seq=1,
        leased_by="other-worker",
        lease_expires_at=lease,
        heartbeat_at=datetime.now(UTC),
    )
    return job_id


async def until(check: Callable[[], bool | Awaitable[bool]], seconds: float = 10) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        result = check()
        if not isinstance(result, bool):
            result = await result
        if result:
            return
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.02)


async def until_waiting_on_the_owner_lock(env: JobsEnv, seconds: float = 10) -> None:
    """Until an app-role backend waits for an advisory lock (the owner lock): polled in
    ``pg_stat_activity``, never a fixed sleep."""
    await until(
        lambda: bool(
            env.sql(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = 'ownvitals_app' "
                "AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
            ).scalar_one()
        ),
        seconds,
    )
