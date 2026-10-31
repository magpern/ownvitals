"""Runs end to end with the test-only kind (ADR 0005 sections 2 to 6): completion and its store,
retries and categories, cancel, a crash and the reaper, the hand-back, and errors that stop the
worker instead of failing the job. Synthetic jobs only."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from jobcore import (
    JobsEnv,
    Upstream,
    claim,
    jenv_fixture,  # noqa: F401 - fixtures
    registry_for,
    run_next,
)
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.db import DatabaseRoleError
from ownvitals.jobs import (
    Complete,
    FencedSession,
    JobFailure,
    JobRun,
    KindRegistry,
    LeaseLost,
    fenced_session,
    queue,
    transitions,
)
from ownvitals.jobs.runner import Execution, execute, hand_back

pytestmark = pytest.mark.usefixtures("jenv")

DRIFT = "the job queue does not match ADR 0005 section 3: planted by the test"
REMAINING = "SELECT extract(epoch FROM lease_expires_at - now()) FROM jobs WHERE id = :id"


def ready(env: JobsEnv, job_id: uuid.UUID) -> None:
    """Backoff is real time; the tests make the requeued job claimable at once."""
    env.sql("UPDATE jobs SET available_at = now() WHERE id = :id", id=job_id)


async def store_progress(fs: FencedSession) -> None:
    """A store that writes in the final transaction (from M1: health rows and cursors)."""
    await fs.event("progress", {"done": 3, "total": 3})


async def test_a_run_completes_with_its_store_in_the_final_transaction(jenv: JobsEnv) -> None:
    _, job_id = jenv.job()
    upstream = Upstream()

    async def run(job_run: JobRun) -> Complete:
        await job_run.checkpoint()
        await job_run.progress(1, 3)
        async with job_run.provider_call(10):
            await upstream.call()
        await job_run.progress(2, 3)
        return Complete(store_progress)

    execution = await run_next(jenv, registry_for(run))
    assert (execution.result, execution.settled) == ("completed", True)
    row = jenv.row(job_id)
    assert (row.state, row.attempt, row.error_category) == ("completed", 1, None)
    assert (row.leased_by, row.lease_expires_at, row.heartbeat_at) == (None, None, None)
    assert row.finished_at is not None
    assert upstream.calls == 1
    assert jenv.events(job_id) == [
        (1, "claimed", {"attempt": 1}, 1),
        (2, "progress", {"done": 1, "total": 3}, 1),
        (3, "progress", {"done": 2, "total": 3}, 1),
        (4, "progress", {"done": 3, "total": 3}, 1),
        (5, "completed", {}, 1),
    ]
    assert row.next_seq == 5


async def test_retryable_failures_use_their_attempts_then_fail(jenv: JobsEnv) -> None:
    _, job_id = jenv.job(max_attempts=2)

    async def run(job_run: JobRun) -> Complete:
        raise JobFailure("upstream_unavailable")

    registry = registry_for(run)
    await run_next(jenv, registry)
    row = jenv.row(job_id)
    assert (row.state, row.attempt, row.lease_version) == ("queued", 1, 2)
    assert row.available_at > row.updated_at  # backoff
    seq, kind, payload, _ = jenv.events(job_id)[-1]
    assert (seq, kind, payload["category"]) == (2, "retry_scheduled", "upstream_unavailable")
    assert set(payload) == {"category", "available_at"}
    ready(jenv, job_id)
    await run_next(jenv, registry)
    row = jenv.row(job_id)
    assert (row.state, row.error_category, row.attempt) == ("failed", "upstream_unavailable", 2)
    [*_, (_, kind, payload, _)] = jenv.events(job_id)
    assert (kind, payload) == ("failed", {"category": "upstream_unavailable"})


@pytest.mark.parametrize(
    ("raised", "category"),
    [
        (JobFailure("not_connected"), "not_connected"),
        (JobFailure("needs_reconnect"), "needs_reconnect"),
        (JobFailure("key_unavailable"), "key_unavailable"),
        (JobFailure("outbound_blocked"), "outbound_blocked"),
        (JobFailure("made_up_category"), "internal"),
        (RuntimeError("a secret-looking text synthetic-token-123"), "internal"),
        (ValueError("synthetic-token-123"), "internal"),
    ],
)
async def test_non_retryable_failures_fail_at_once_with_the_category_only(
    jenv: JobsEnv, raised: Exception, category: str, caplog: pytest.LogCaptureFixture
) -> None:
    _, job_id = jenv.job()
    caplog.set_level("INFO")

    async def run(job_run: JobRun) -> Complete:
        raise raised

    await run_next(jenv, registry_for(run))
    row = jenv.row(job_id)
    assert (row.state, row.error_category, row.attempt) == ("failed", category, 1)
    assert "synthetic-token-123" not in caplog.text
    assert "synthetic-token-123" not in str(jenv.events(job_id))
    for record in caplog.records:
        assert "synthetic-token-123" not in str(record.__dict__)


async def test_a_rate_limit_waits_at_least_its_hint(jenv: JobsEnv) -> None:
    _, job_id = jenv.job()

    async def run(job_run: JobRun) -> Complete:
        raise JobFailure("rate_limited", retry_after=1200)

    await run_next(jenv, registry_for(run))
    wait = jenv.sql(
        "SELECT extract(epoch FROM available_at - now()) FROM jobs WHERE id = :id", id=job_id
    ).scalar_one()
    assert 1190 < wait <= 1200


async def test_invalid_input_fails_without_running(jenv: JobsEnv) -> None:
    _, bad = jenv.job(input='{"unexpected": 1}')
    calls: list[int] = []

    async def run(job_run: JobRun) -> Complete:
        calls.append(1)
        return Complete()

    await run_next(jenv, registry_for(run))
    assert (jenv.row(bad).state, jenv.row(bad).error_category) == ("failed", "invalid_input")
    assert calls == []
    assert jenv.kinds(bad) == ["claimed", "failed"]


async def test_a_kind_no_longer_registered_fails_internal(jenv: JobsEnv) -> None:
    _, job_id = jenv.job()
    execution = await claim(jenv)
    await execute(execution, jenv.factory, KindRegistry(), jenv.config(), asyncio.Event())
    assert (jenv.row(job_id).state, jenv.row(job_id).error_category) == ("failed", "internal")


async def test_a_handler_returning_no_outcome_fails_internal(jenv: JobsEnv) -> None:
    _, job_id = jenv.job()

    async def run(job_run: JobRun) -> Any:
        return "done"

    await run_next(jenv, registry_for(run))
    assert (jenv.row(job_id).state, jenv.row(job_id).error_category) == ("failed", "internal")


@pytest.mark.parametrize(
    ("raised", "state", "category"),
    [
        (ValueError("bug"), "failed", "internal"),
        (JobFailure("not_connected"), "failed", "not_connected"),
        (JobFailure("upstream_unavailable"), "queued", None),
        (OperationalError("COMMIT", {}, ConnectionError()), "queued", None),
    ],
)
async def test_a_failing_store_rolls_back_and_counts_as_the_attempts_failure(
    jenv: JobsEnv, raised: Exception, state: str, category: str | None
) -> None:
    """What ``store`` wrote goes with it; a database blip there is retried as
    ``upstream_unavailable`` (ADR 0005 section 6), anything else is ``internal``."""
    _, job_id = jenv.job()

    async def broken(fs: FencedSession) -> None:
        await store_progress(fs)
        raise raised

    async def run(job_run: JobRun) -> Complete:
        return Complete(broken)

    await run_next(jenv, registry_for(run))
    row = jenv.row(job_id)
    assert (row.state, row.error_category) == (state, category)
    assert "progress" not in jenv.kinds(job_id)  # the store's write was rolled back
    if state == "queued":
        assert jenv.events(job_id)[-1][2]["category"] == "upstream_unavailable"


@pytest.mark.parametrize("before_store", ["cancel", "lease_lost"])
async def test_the_final_transaction_sees_a_cancel_or_a_lost_lease(
    jenv: JobsEnv, before_store: str
) -> None:
    _, job_id = jenv.job()
    calls: list[str] = []

    async def store(fs: FencedSession) -> None:
        calls.append("store")

    async def run(job_run: JobRun) -> Complete:
        if before_store == "cancel":
            jenv.sql("UPDATE jobs SET cancel_requested_at = now() WHERE id = :id", id=job_id)
        else:
            jenv.sql("UPDATE jobs SET lease_version = lease_version + 1 WHERE id = :id", id=job_id)
        return Complete(store)

    execution = await run_next(jenv, registry_for(run))
    assert calls == []
    expected = {"cancel": "cancelled", "lease_lost": "lost"}[before_store]
    assert (execution.result, execution.settled) == (expected, True)
    row = jenv.row(job_id)
    assert row.state == ("cancelled" if before_store == "cancel" else "running")


async def test_a_cancel_stops_the_run_at_its_next_checkpoint(jenv: JobsEnv) -> None:
    _, job_id = jenv.job()
    reached: list[int] = []
    upstream = Upstream()

    async def run(job_run: JobRun) -> Complete:
        await job_run.progress(1, 3)
        jenv.sql("UPDATE jobs SET cancel_requested_at = now() WHERE id = :id", id=job_id)
        reached.append(1)
        async with job_run.provider_call(10):  # refused: no call after a cancel
            await upstream.call()
        reached.append(2)
        return Complete()

    execution = await run_next(jenv, registry_for(run))
    assert reached == [1]
    assert upstream.calls == 0
    assert execution.result == "cancelled"
    row = jenv.row(job_id)
    assert (row.state, row.error_category, row.finished_at is not None) == (
        "cancelled",
        None,
        True,
    )
    assert jenv.kinds(job_id) == ["claimed", "progress", "cancelled"]


async def test_a_retry_with_a_cancel_requested_ends_cancelled(jenv: JobsEnv) -> None:
    _, job_id = jenv.job()

    async def run(job_run: JobRun) -> Complete:
        jenv.sql("UPDATE jobs SET cancel_requested_at = now() WHERE id = :id", id=job_id)
        raise JobFailure("upstream_unavailable")

    await run_next(jenv, registry_for(run))
    assert jenv.row(job_id).state == "cancelled"
    assert "retry_scheduled" not in jenv.kinds(job_id)


async def test_a_terminal_job_never_changes_again(jenv: JobsEnv) -> None:
    _, job_id = jenv.job()
    execution = await run_next(jenv, registry_for(lambda job_run: _complete()))
    assert execution.lease is not None
    before = (jenv.row(job_id), jenv.events(job_id))
    for write in (
        transitions.complete,
        transitions.cancel,
        transitions.hand_back,
        lambda fs: transitions.fail(fs, "internal"),
        lambda fs: transitions.retry(fs, "upstream_unavailable", 0),
    ):
        with pytest.raises(LeaseLost):
            async with fenced_session(jenv.factory, execution.lease) as fs:
                await write(fs)
    assert (jenv.row(job_id), jenv.events(job_id)) == before


async def _complete() -> Complete:
    return Complete()


DRIFT_ENDINGS = ["outcome", "job_failure"]


@pytest.mark.parametrize("ending", DRIFT_ENDINGS)
async def test_a_role_error_met_by_the_heartbeat_is_raised_not_stored_or_retried(
    jenv: JobsEnv, ending: str
) -> None:
    """The heartbeat met ``DatabaseRoleError`` and the run still ended on its own: with an outcome
    (the abandon came too late) or with a ``JobFailure`` from the call it waited for. Either way
    the runner raises it (the worker stops and hands the lease back): no store, no retry."""
    _, job_id = jenv.job()
    calls: list[str] = []

    async def store(fs: FencedSession) -> None:
        calls.append("store")

    async def run(job_run: JobRun) -> Complete:
        if ending == "job_failure":
            async with job_run.provider_call(5):
                job_run.mark_fatal(DatabaseRoleError(DRIFT))
                raise JobFailure("upstream_unavailable")  # the call failed
        job_run.fatal = DatabaseRoleError(DRIFT)  # as if met after the kind returned
        return Complete(store)

    execution = await claim(jenv)
    with pytest.raises(DatabaseRoleError, match="planted"):
        await execute(execution, jenv.factory, registry_for(run), jenv.config(), asyncio.Event())
    assert calls == []
    assert execution.needs_hand_back()
    row = jenv.row(job_id)
    assert (row.state, row.attempt, row.error_category) == ("running", 1, None)
    assert "retry_scheduled" not in jenv.kinds(job_id)


@pytest.mark.parametrize("where", ["checkpoint", "progress", "fenced", "provider_call"])
async def test_a_database_blip_inside_the_run_retries_the_attempt(
    jenv: JobsEnv, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """A blip during a checkpoint, a progress write, a kind's own fenced write or the lease
    extension of a provider call is retryable (``upstream_unavailable``), not ``internal``."""
    from ownvitals.jobs import run as run_module

    _, job_id = jenv.job()
    real = run_module.fenced_session
    calls = {"n": 0}

    @contextlib.asynccontextmanager
    async def blinking(factory: Any, lease: Any) -> AsyncIterator[FencedSession]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("UPDATE jobs", {}, ConnectionError())
        async with real(factory, lease) as fs:
            yield fs

    async def run(job_run: JobRun) -> Complete:
        if where == "checkpoint":
            await job_run.checkpoint()
        elif where == "progress":
            await job_run.progress(1, 2)
        elif where == "fenced":
            async with job_run.fenced():
                pass
        else:
            async with job_run.provider_call(5):
                pytest.fail("no call after a failed lease extension")
        return Complete()

    monkeypatch.setattr(run_module, "fenced_session", blinking)
    await run_next(jenv, registry_for(run))
    row = jenv.row(job_id)
    assert (row.state, row.attempt, row.error_category) == ("queued", 1, None)
    [*_, (_, kind, payload, _)] = jenv.events(job_id)
    assert (kind, payload["category"]) == ("retry_scheduled", "upstream_unavailable")


async def test_a_crash_mid_run_is_reaped_and_the_retry_completes(jenv: JobsEnv) -> None:
    _, job_id = jenv.job()
    started = asyncio.Event()

    async def hang(job_run: JobRun) -> Complete:
        await job_run.progress(1, 2)
        started.set()
        await asyncio.sleep(60)
        return Complete()

    execution = await claim(jenv)
    task = asyncio.ensure_future(
        execute(execution, jenv.factory, registry_for(hang), jenv.config(), asyncio.Event())
    )
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()  # the worker dies: no hand-back, nothing recorded
    with pytest.raises(asyncio.CancelledError):
        await task
    assert jenv.row(job_id).state == "running"
    jenv.expire_lease(job_id)
    assert await queue.reap(jenv.factory) == 1
    row = jenv.row(job_id)
    assert (row.state, row.attempt) == ("queued", 1)  # the crashed attempt counts
    ready(jenv, job_id)

    async def finish(job_run: JobRun) -> Complete:
        await job_run.progress(2, 2)
        return Complete()

    await run_next(jenv, registry_for(finish))
    row = jenv.row(job_id)
    assert (row.state, row.attempt, row.lease_version) == ("completed", 2, 3)


async def test_a_crash_mid_provider_call_leaves_the_lease_to_the_reaper(jenv: JobsEnv) -> None:
    _, job_id = jenv.job()
    in_call = asyncio.Event()

    async def call(job_run: JobRun) -> Complete:
        async with job_run.provider_call(30):
            in_call.set()
            await asyncio.sleep(60)  # the provider call
        return Complete()

    execution = await claim(jenv)
    task = asyncio.ensure_future(
        execute(execution, jenv.factory, registry_for(call), jenv.config(), asyncio.Event())
    )
    await asyncio.wait_for(in_call.wait(), 5)
    assert jenv.sql(REMAINING, id=job_id).scalar_one() > 55  # the call's 30 s plus 30 s
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    jenv.expire_lease(job_id)
    assert await queue.reap(jenv.factory) == 1
    assert jenv.row(job_id).lease_version == 2


async def test_a_hand_back_before_the_context_was_issued_requeues_the_job(jenv: JobsEnv) -> None:
    """Shutdown right after a claim: the hand-back uses the owner's bare context and touches only
    the job row (ADR 0005 section 5)."""
    owner, job_id = jenv.job()
    execution = await claim(jenv)
    assert execution.lease is None
    await hand_back(execution, jenv.factory)
    assert execution.lease is not None
    assert (execution.lease.ctx.user_id, execution.lease.ctx.role) == (owner, "user")
    assert execution.lease.ctx.connections == frozenset()
    row = jenv.row(job_id)
    assert (row.state, row.attempt, row.lease_version) == ("queued", 0, 2)
    assert jenv.kinds(job_id) == ["claimed", "requeued"]
    await hand_back(Execution(execution.claimed), jenv.factory)  # a second one finds it gone
    assert jenv.kinds(job_id) == ["claimed", "requeued"]


async def test_a_shutdown_hand_back_with_a_cancel_requested_ends_cancelled(
    jenv: JobsEnv,
) -> None:
    _, job_id = jenv.job()
    execution = await claim(jenv)
    jenv.sql("UPDATE jobs SET cancel_requested_at = now() WHERE id = :id", id=job_id)
    await hand_back(execution, jenv.factory)
    assert (jenv.row(job_id).state, execution.result) == ("cancelled", "cancelled")


async def test_a_shutdown_stops_the_run_at_its_checkpoint_and_hands_it_back(
    jenv: JobsEnv,
) -> None:
    _, job_id = jenv.job()
    shutdown = asyncio.Event()

    async def run(job_run: JobRun) -> Complete:
        shutdown.set()
        await job_run.checkpoint()
        pytest.fail("the checkpoint stops the run")

    await run_next(jenv, registry_for(run), shutdown=shutdown)
    row = jenv.row(job_id)
    assert (row.state, row.attempt) == ("queued", 0)
    assert jenv.kinds(job_id)[-1] == "requeued"
