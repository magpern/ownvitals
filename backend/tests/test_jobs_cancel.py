"""The user's cancel against a running worker (ADR 0005 sections 2 and 6), through
``ownvitals.jobs.service`` as the jobs API calls it. Synthetic jobs only."""

import asyncio
from uuid import UUID, uuid4

from ctx import make_context
from jobcore import (
    JobsEnv,
    Upstream,
    claim,
    jenv_fixture,  # noqa: F401 - fixtures
    registry_for,
    run_next,
)
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.jobs import Complete, FencedSession, JobFailure, JobRun, service
from ownvitals.jobs.runner import execute


async def user_cancel(env: JobsEnv, owner: UUID, job_id: UUID) -> str | None:
    view = await service.cancel(env.factory, make_context(owner), job_id)
    return None if view is None else view.state


async def test_a_queued_job_cancels_at_once_and_idempotently(jenv: JobsEnv) -> None:
    owner, job_id = jenv.job()
    assert await user_cancel(jenv, owner, job_id) == "cancelled"
    assert await user_cancel(jenv, owner, job_id) == "cancelled"
    row = jenv.row(job_id)
    assert (row.finished_at is not None, row.cancel_requested_at is not None) == (True, True)
    assert jenv.kinds(job_id) == ["cancelled"]  # no second event
    assert await user_cancel(jenv, uuid4(), job_id) is None  # another user's job: not found
    assert await claim_none(jenv)


async def claim_none(env: JobsEnv) -> bool:
    from ownvitals.jobs import queue

    return await queue.claim(env.factory, "w", 60, ["test_kind"]) is None


async def test_a_running_job_stops_at_its_next_checkpoint_without_another_call(
    jenv: JobsEnv,
) -> None:
    owner, job_id = jenv.job()
    upstream = Upstream()
    turn_done = asyncio.Event()
    cancelled = asyncio.Event()

    async def run(job_run: JobRun) -> Complete:
        for _ in range(5):  # provider calls, each behind a checkpoint
            async with job_run.provider_call(10):
                await upstream.call()
            if upstream.calls == 2:
                turn_done.set()
                await cancelled.wait()
        return Complete()

    execution = await claim(jenv)
    task = asyncio.ensure_future(
        execute(execution, jenv.factory, registry_for(run), jenv.config(), asyncio.Event())
    )
    await asyncio.wait_for(turn_done.wait(), 5)
    assert await user_cancel(jenv, owner, job_id) == "running"
    assert await user_cancel(jenv, owner, job_id) == "running"  # requested once
    cancelled.set()
    await asyncio.wait_for(task, 5)
    row = jenv.row(job_id)
    assert (row.state, row.finished_at is not None) == ("cancelled", True)
    assert upstream.calls == 2
    assert jenv.kinds(job_id) == ["claimed", "cancel_requested", "cancelled"]


async def test_a_cancel_racing_completion_ends_in_exactly_one_terminal_state(
    jenv: JobsEnv,
) -> None:
    for _ in range(6):
        owner, job_id = jenv.job()
        go = asyncio.Event()
        stored: list[UUID] = []

        async def store(
            fs: FencedSession, job_id: UUID = job_id, stored: list[UUID] = stored
        ) -> None:
            stored.append(job_id)

        async def run(job_run: JobRun, go: asyncio.Event = go) -> Complete:
            await go.wait()
            return Complete(store)

        execution = await claim(jenv)
        task = asyncio.ensure_future(
            execute(execution, jenv.factory, registry_for(run), jenv.config(), asyncio.Event())
        )
        go.set()
        await asyncio.gather(task, user_cancel(jenv, owner, job_id))
        row = jenv.row(job_id)
        assert row.state in ("completed", "cancelled")
        terminal = [k for k in jenv.kinds(job_id) if k in ("completed", "cancelled", "failed")]
        assert len(terminal) == 1
        assert (stored == [job_id]) == (row.state == "completed")


async def test_a_retry_of_a_job_with_a_cancel_requested_cancels_it(jenv: JobsEnv) -> None:
    owner, job_id = jenv.job()

    async def run(job_run: JobRun) -> Complete:
        await user_cancel(jenv, owner, job_id)
        raise JobFailure("upstream_unavailable")

    await run_next(jenv, registry_for(run))
    assert jenv.row(job_id).state == "cancelled"
    assert jenv.kinds(job_id) == ["claimed", "cancel_requested", "cancelled"]
