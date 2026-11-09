"""RLS for the tool set (ADR 0014 sections 4 and 8; ADR 0002 section 4; #206): a tool run for
user A never returns user B's data, in the request context (A's ``UserContext`` as web issues
it) and in the worker context (a job of A, claimed and run by the worker's runner, whose issuer
builds the context from the claimed row alone).

Two synthetic accounts with distinct values: every reading of A is near 70 kg / 60 bpm, every
reading of B near 120 kg / 95 bpm, so a value of B in A's output shows.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from authtools import Env, auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from jobcore import settings_for
from jobtools import admin_url, engine_for, new_job
from ownvitals.ai.tools import run_tool
from ownvitals.auth.owners import ACCOUNTS
from ownvitals.db import create_session_factory
from ownvitals.jobs import Complete, JobKind, JobRun, KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.worker.loop import create_worker_engine
from test_ai_tools_plant import (  # noqa: F401 - fixture
    CALLS,
    Planted,
    ai_env_fixture,
    call_all,
    forbidden_strings,
    plant_account,
)
from viewtools import NOW

SYNTHETIC = True
KIND = "ai_tool_probe"


class NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _two(env: Env) -> tuple[Planted, Planted]:
    a = await plant_account(env, "synthetic-a@example.com", weight=70, pulse=60, grpid=100_000)
    b = await plant_account(env, "synthetic-b@example.com", weight=120, pulse=95, grpid=200_000)
    return a, b


def _assert_only_own(
    results: list[tuple[str, dict[str, Any], dict[str, Any]]],
    other: Planted,
    low: float,
    high: float,
) -> None:
    blob = json.dumps([r for _, _, r in results])
    for value in forbidden_strings(other):
        assert value not in blob, value
    for name, args, result in results:
        assert "error" not in result, (name, args, result)
        if name == "daily_values":
            values = [p["value"] for p in result["points"]]
            assert values, args
            assert all(low <= v <= high for v in values), (args, values)
        if name == "metric_summary" and result["median"] is not None:
            assert low <= result["median"] <= high, (args, result["median"])


A_RANGE = (59.0, 71.0)  # A's pulses 60..63 and weights 70..70.2
B_RANGE = (94.0, 121.0)  # B's pulses 95..98 and weights 120..120.2


async def test_the_request_context_reads_only_the_callers_data(ai_env: Env) -> None:
    a, b = await _two(ai_env)
    _assert_only_own(await call_all(ai_env, a.ctx), b, *A_RANGE)
    _assert_only_own(await call_all(ai_env, b.ctx), a, *B_RANGE)


@pytest.fixture(name="worker_factory")
async def worker_factory_fixture(ai_env: Env, app_database_url: str) -> AsyncIterator[Any]:
    engine = create_worker_engine(settings_for(app_database_url), pool_size=4)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def _run_in_worker(
    env: Env, factory: Any, planted: Planted
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    with engine_for(admin_url()) as engine, engine.begin() as conn:
        new_job(conn, planted.ctx.user_id, kind=KIND, input="{}")
    seen: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def run(job_run: JobRun) -> Complete:
        assert job_run.ctx.user_id == planted.ctx.user_id
        for name, args in CALLS:
            result = await run_tool(job_run.factory, job_run.ctx, env.settings, name, args, now=NOW)
            seen.append((name, args, result))
        return Complete()

    registry = KindRegistry()
    registry.register(JobKind(KIND, NoInput, run))
    claimed = await queue.claim(factory, "ai-tool-probe-worker", 60, [KIND])
    assert claimed is not None
    execution = Execution(claimed)
    await execute(
        execution,
        factory,
        registry,
        RunConfig(lease_seconds=60, accounts=ACCOUNTS),
        asyncio.Event(),
    )
    assert execution.result == "completed", execution.result
    return seen


async def test_the_worker_context_reads_only_the_job_owners_data(
    ai_env: Env, worker_factory: Any
) -> None:
    a, b = await _two(ai_env)
    _assert_only_own(await _run_in_worker(ai_env, worker_factory, a), b, *A_RANGE)
    _assert_only_own(await _run_in_worker(ai_env, worker_factory, b), a, *B_RANGE)
