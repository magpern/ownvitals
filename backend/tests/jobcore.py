"""Helpers for the jobs-core tests (ADR 0005 sections 2 to 6): the app role's async engine (with
the worker's queue check), the test-only kind and a fake account directory.

Setup rows are written as the superuser (``jobtools``); everything under test runs as
``ownvitals_app`` through ``ownvitals.jobs``. Synthetic data only.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jobtools import KIND, events, job, new_job, new_user
from ownvitals.config import Settings
from ownvitals.context import UserContext
from ownvitals.db import create_session_factory
from ownvitals.jobs import JobKind, JobRun, KindRegistry, Outcome, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.worker.loop import create_worker_engine

Factory = async_sessionmaker[AsyncSession]


class KindInput(BaseModel):
    """The test-only kind's input (synthetic)."""

    model_config = ConfigDict(extra="forbid")
    note: str = "fine"


@dataclass
class FakeAccounts:
    """The account directory a test controls (``ownvitals.jobs.issuer.Accounts``)."""

    disabled: set[UUID] = field(default_factory=set[UUID])
    refs: frozenset[tuple[str, UUID]] = frozenset()
    asked: list[UUID] = field(default_factory=list[UUID])

    async def owner_active(self, factory: Factory, owner_id: UUID) -> bool:
        self.asked.append(owner_id)
        return owner_id not in self.disabled

    async def connection_refs(
        self, factory: Factory, ctx: UserContext
    ) -> frozenset[tuple[str, UUID]]:
        return self.refs


def settings_for(app_url: str, **values: Any) -> Settings:
    return Settings(  # pyright: ignore[reportCallIssue]
        database_url=app_url,  # pyright: ignore[reportArgumentType]
        environment="test",
        **{"job_lease_seconds": 60, **values},
    )


@dataclass
class JobsEnv:
    settings: Settings
    engine: AsyncEngine
    factory: Factory
    admin: Connection
    accounts: FakeAccounts

    def job(self, owner: UUID | None = None, **columns: Any) -> tuple[UUID, UUID]:
        owner = owner or new_user()
        return owner, new_job(self.admin, owner, **columns)

    def row(self, job_id: UUID) -> Any:
        return job(self.admin, job_id)

    def events(self, job_id: UUID) -> list[tuple[int, str, dict[str, Any], int]]:
        return events(self.admin, job_id)

    def kinds(self, job_id: UUID) -> list[str]:
        return [kind for _, kind, _, _ in self.events(job_id)]

    def sql(self, statement: str, **params: Any) -> Any:
        return self.admin.execute(text(statement), params)

    def expire_lease(self, job_id: UUID) -> None:
        self.sql(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id",
            id=job_id,
        )

    def config(self) -> RunConfig:
        return RunConfig(lease_seconds=self.settings.job_lease_seconds, accounts=self.accounts)


@pytest.fixture(name="jenv")
async def jenv_fixture(admin: Connection, app_database_url: str) -> AsyncIterator[JobsEnv]:
    settings = settings_for(app_database_url)
    engine = create_worker_engine(settings, pool_size=10)
    try:
        yield JobsEnv(settings, engine, create_session_factory(engine), admin, FakeAccounts())
    finally:
        await engine.dispose()


def registry_for(run: Callable[[JobRun], Awaitable[Outcome]]) -> KindRegistry:
    registry = KindRegistry()
    registry.register(JobKind(KIND, KindInput, run))
    return registry


@dataclass
class Upstream:
    """A fake provider: counts the calls a run makes (no network)."""

    calls: int = 0

    async def call(self) -> int:
        self.calls += 1
        await asyncio.sleep(0)
        return self.calls


async def claim(env: JobsEnv, worker: str = "test-w") -> Execution:
    row = await queue.claim(env.factory, worker, env.settings.job_lease_seconds, [KIND])
    assert row is not None, "nothing to claim"
    return Execution(row)


async def run_next(
    env: JobsEnv,
    registry: KindRegistry,
    *,
    shutdown: asyncio.Event | None = None,
    config: RunConfig | None = None,
) -> Execution:
    """Claim one test job and run it to the end of its attempt."""
    execution = await claim(env)
    await execute(
        execution, env.factory, registry, config or env.config(), shutdown or asyncio.Event()
    )
    return execution
