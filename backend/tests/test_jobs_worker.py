"""The worker loop (ADR 0005 sections 3 and 5): two workers never run one job at once, nothing is
claimed before the queue check passed, LISTEN with a poll fallback, the reaper, a poison row, the
shutdown hand-back and a drifted queue stopping the worker.

Acceptance criterion 2 of #9 is ``test_two_workers_never_run_one_job_at_once``. Synthetic jobs of
the test-only kind.
"""

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from jobapi import until
from jobcore import (
    JobsEnv,
    Upstream,
    jenv_fixture,  # noqa: F401 - fixtures
    registry_for,
    settings_for,
)
from jobtools import admin_fixture, install_queue, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.db import DatabaseRoleError
from ownvitals.db.queue_check import QueueDriftError
from ownvitals.jobs import Complete, JobRun, KindRegistry, queue
from ownvitals.worker import loop as worker_loop
from ownvitals.worker.loop import Worker, create_worker_engine

pytestmark = pytest.mark.usefixtures("jenv")

DRIFT = "the job queue does not match ADR 0005 section 3: planted by the test"


@contextlib.asynccontextmanager
async def running(
    env: JobsEnv, registry: KindRegistry, **settings: Any
) -> AsyncIterator[tuple[Worker, asyncio.Event, asyncio.Task[None]]]:
    values: dict[str, Any] = {"job_lease_seconds": 60, "worker_poll_seconds": 0.1, **settings}
    config = settings_for(env.settings.database_url.get_secret_value(), **values)
    engine = create_worker_engine(config)
    worker = Worker(config, engine, registry=registry, accounts=env.accounts)
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run(stop))
    try:
        yield worker, stop, task
    finally:
        stop.set()
        stopped_by_drift = (
            task.done() and not task.cancelled() and isinstance(task.exception(), DatabaseRoleError)
        )
        if not stopped_by_drift:  # a drift the test expected was raised to it already
            await asyncio.wait_for(task, 40)
        await engine.dispose()


def done_run(calls: list[uuid.UUID] | None = None) -> Callable[[JobRun], Awaitable[Complete]]:
    async def run(job_run: JobRun) -> Complete:
        if calls is not None:
            calls.append(job_run.lease.job_id)
        await job_run.progress(1, 1)
        return Complete()

    return run


async def test_two_workers_never_run_one_job_at_once(jenv: JobsEnv) -> None:
    """Acceptance 2: two workers (two engines, two concurrent slots each) drain 24 jobs of four
    users. Every job runs exactly once, by one worker, and no two runs of a job ever overlap."""
    users = [uuid.uuid4() for _ in range(4)]
    jobs = {jenv.job(users[i % 4])[1] for i in range(24)}
    active: dict[uuid.UUID, int] = {}
    overlaps: list[uuid.UUID] = []
    ran: list[tuple[uuid.UUID, str]] = []

    def kind_for(name: str) -> KindRegistry:
        async def run(job_run: JobRun) -> Complete:
            job_id = job_run.lease.job_id
            active[job_id] = active.get(job_id, 0) + 1
            if active[job_id] > 1:
                overlaps.append(job_id)
            ran.append((job_id, name))
            try:
                await job_run.checkpoint()
                await asyncio.sleep(0.05)
                await job_run.progress(1, 1)
            finally:
                active[job_id] -= 1
            return Complete()

        return registry_for(run)

    async with (
        running(jenv, kind_for("a"), worker_concurrency=2) as (a, _, _),
        running(jenv, kind_for("b"), worker_concurrency=2) as (b, _, _),
    ):
        await until(lambda: all(jenv.row(j).state == "completed" for j in jobs), seconds=30)
        await until(lambda: not a.running and not b.running)
    assert overlaps == []
    assert sorted(job_id for job_id, _ in ran) == sorted(jobs)  # each exactly once
    assert {name for _, name in ran} == {"a", "b"}  # both workers took part
    for job_id in jobs:
        row = jenv.row(job_id)
        assert (row.attempt, row.lease_version) == (1, 1)
        assert jenv.kinds(job_id) == ["claimed", "progress", "completed"]


async def test_nothing_is_claimed_before_the_queue_check_passed(
    jenv: JobsEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, job_id = jenv.job()

    async def no_verify(engine: object) -> None:
        return None

    with monkeypatch.context() as patched:  # not undo(): that would also undo the test guards
        patched.setattr(worker_loop, "queue_verified", lambda engine: False)
        patched.setattr(worker_loop, "verify_database_role", no_verify)
        async with running(jenv, registry_for(done_run())):
            await asyncio.sleep(0.5)
            assert jenv.row(job_id).state == "queued"
    async with running(jenv, registry_for(done_run())):
        await until(lambda: jenv.row(job_id).state == "completed")


async def test_the_shipped_registry_claims_nothing(jenv: JobsEnv) -> None:
    """#9 registers no kind (``KINDS`` is empty): the worker claims nothing."""
    from ownvitals.jobs import KINDS

    assert KINDS.names() == []
    _, job_id = jenv.job()
    async with running(jenv, KINDS) as (worker, _, _):
        await until(lambda: worker.passes > 3)
        assert jenv.row(job_id).state == "queued"


def listening(env: JobsEnv) -> int:
    """Backends of the app role whose last statement was the worker's ``LISTEN``."""
    return env.sql(
        "SELECT count(*) FROM pg_stat_activity WHERE usename = 'ownvitals_app' "
        "AND query = 'LISTEN jobs_queued' AND state = 'idle'"
    ).scalar_one()


async def test_a_notification_wakes_the_worker_before_its_poll(jenv: JobsEnv) -> None:
    owner = uuid.uuid4()
    before = listening(jenv)
    async with running(jenv, registry_for(done_run()), worker_poll_seconds=30) as (worker, _, _):
        await until(lambda: listening(jenv) > before)  # the listener is up
        await until(lambda: worker.passes >= 1)
        _, job_id = jenv.job(owner)
        started = time.monotonic()
        jenv.sql("SELECT pg_notify('jobs_queued', '')")
        await until(lambda: jenv.row(job_id).state == "completed", seconds=10)
        assert time.monotonic() - started < 10  # well before the 30 s poll
        await until(lambda: not worker.running)  # the task ends just after its commit


async def test_a_poison_row_does_not_block_other_users_and_is_failed(jenv: JobsEnv) -> None:
    _, poison = jenv.job(attempt=3, max_attempts=3)
    jenv.sql("UPDATE jobs SET available_at = now() - interval '1 hour' WHERE id = :id", id=poison)
    others = [jenv.job()[1] for _ in range(3)]
    calls: list[uuid.UUID] = []
    async with running(jenv, registry_for(done_run(calls))):
        await until(lambda: all(jenv.row(j).state == "completed" for j in others))
        await until(lambda: jenv.row(poison).state == "failed")
    assert jenv.row(poison).error_category == "internal"
    assert sorted(calls) == sorted(others)


async def test_maintenance_reaps_a_lost_lease(jenv: JobsEnv) -> None:
    _, job_id = jenv.job()
    assert await queue.claim(jenv.factory, "gone-worker", 60, ["test_kind"]) is not None
    jenv.expire_lease(job_id)
    async with running(jenv, KindRegistry()) as (worker, _, _):
        await until(lambda: jenv.row(job_id).state == "queued")
        await worker.maintain()  # nothing more to reap
    row = jenv.row(job_id)
    assert (row.attempt, row.lease_version, row.leased_by) == (1, 2, None)
    assert jenv.kinds(job_id) == ["claimed", "retry_scheduled"]


async def test_the_concurrency_limit_holds(jenv: JobsEnv) -> None:
    gate = asyncio.Event()
    peak = 0
    live = 0

    async def run(job_run: JobRun) -> Complete:
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        await gate.wait()
        live -= 1
        return Complete()

    jobs = [jenv.job()[1] for _ in range(5)]
    async with running(jenv, registry_for(run), worker_concurrency=2) as (worker, _, _):
        await until(lambda: len(worker.running) == 2)
        await asyncio.sleep(0.4)
        assert peak == 2
        gate.set()
        await until(lambda: all(jenv.row(j).state == "completed" for j in jobs))


async def test_shutdown_hands_the_lease_back_without_an_attempt_and_no_new_call(
    jenv: JobsEnv,
) -> None:
    _, job_id = jenv.job()
    upstream = Upstream()
    in_call = asyncio.Event()

    async def run(job_run: JobRun) -> Complete:
        async with job_run.provider_call(10):
            await upstream.call()  # a provider call in flight when SIGTERM comes
            in_call.set()
            await asyncio.sleep(0.3)
        async with job_run.provider_call(10):  # refused: shutdown began
            await upstream.call()
        return Complete()

    async with running(jenv, registry_for(run)) as (_, stop, task):
        await asyncio.wait_for(in_call.wait(), 10)
        lease_version = jenv.row(job_id).lease_version
        stop.set()
        await asyncio.wait_for(task, 30)
    row = jenv.row(job_id)
    assert (row.state, row.attempt, row.leased_by) == ("queued", 0, None)
    assert row.lease_version == lease_version + 1
    assert upstream.calls == 1
    assert jenv.kinds(job_id)[-1] == "requeued"


async def test_a_call_still_in_flight_after_the_wait_is_cancelled_and_handed_back(
    jenv: JobsEnv,
) -> None:
    _, job_id = jenv.job()
    in_call = asyncio.Event()

    async def run(job_run: JobRun) -> Complete:
        async with job_run.provider_call(30):
            in_call.set()
            await asyncio.sleep(60)  # a slow provider
        return Complete()

    async with running(jenv, registry_for(run), worker_shutdown_wait_seconds=0.3) as (
        _,
        stop,
        task,
    ):
        await asyncio.wait_for(in_call.wait(), 10)
        started = time.monotonic()
        stop.set()
        await asyncio.wait_for(task, 30)
        assert time.monotonic() - started < 5
    row = jenv.row(job_id)
    assert (row.state, row.attempt, row.lease_version) == ("queued", 0, 2)


async def test_a_cancel_requested_before_the_hand_back_ends_the_job_cancelled(
    jenv: JobsEnv,
) -> None:
    _, job_id = jenv.job()
    in_run = asyncio.Event()

    async def run(job_run: JobRun) -> Complete:
        in_run.set()
        await asyncio.sleep(60)
        return Complete()

    async with running(jenv, registry_for(run), worker_shutdown_wait_seconds=0.2) as (
        _,
        stop,
        task,
    ):
        await asyncio.wait_for(in_run.wait(), 10)
        jenv.sql("UPDATE jobs SET cancel_requested_at = now() WHERE id = :id", id=job_id)
        stop.set()
        await asyncio.wait_for(task, 30)
    row = jenv.row(job_id)
    assert (row.state, row.finished_at is not None) == ("cancelled", True)


async def test_sigterm_during_a_claim_claims_nothing_more_and_calls_nothing(
    jenv: JobsEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = [jenv.job()[1] for _ in range(2)]
    upstream = Upstream()
    real_claim = worker_loop.queue.claim
    holder: dict[str, asyncio.Event] = {}

    async def claim_then_sigterm(*args: Any, **kwargs: Any) -> Any:
        holder["stop"].set()  # SIGTERM arrives while this claim is in flight
        await asyncio.sleep(0.05)  # the signal path runs before the claim returns
        return await real_claim(*args, **kwargs)

    async def run(job_run: JobRun) -> Complete:
        async with job_run.provider_call(10):
            await upstream.call()
        return Complete()

    monkeypatch.setattr(worker_loop.queue, "claim", claim_then_sigterm)
    async with running(jenv, registry_for(run), worker_concurrency=2) as (worker, stop, task):
        holder["stop"] = stop
        await asyncio.wait_for(task, 30)
        assert worker.shutdown.is_set()
    rows = [jenv.row(j) for j in jobs]
    assert upstream.calls == 0
    assert sorted(r.lease_version for r in rows) == [0, 2]  # one claimed then handed back
    assert all((r.state, r.attempt) == ("queued", 0) for r in rows)


async def test_a_drifted_queue_stops_the_worker_before_any_claim(
    jenv: JobsEnv, admin: Connection
) -> None:
    """The real per-connection check: a drift planted before the worker starts is refused on its
    first connection, and the worker stops without claiming."""
    _, job_id = jenv.job()
    admin.execute(text("GRANT EXECUTE ON FUNCTION public.queue_stats() TO PUBLIC"))
    try:
        async with running(jenv, registry_for(done_run())) as (worker, _, task):
            with pytest.raises(QueueDriftError, match="queue_stats"):
                await asyncio.wait_for(task, 10)
            assert worker.running == {}
    finally:
        install_queue(admin.engine.url.render_as_string(hide_password=False))
    assert jenv.row(job_id).state == "queued"


async def test_a_drift_found_by_the_background_check_stops_the_worker(
    jenv: JobsEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background check raising ``DatabaseRoleError`` ends ``run``."""
    _, job_id = jenv.job()

    async def drifted(engine: object) -> None:
        raise DatabaseRoleError(DRIFT)

    monkeypatch.setattr(worker_loop, "queue_verified", lambda engine: False)
    monkeypatch.setattr(worker_loop, "verify_database_role", drifted)
    async with running(jenv, registry_for(done_run())) as (worker, _, task):
        with pytest.raises(DatabaseRoleError, match="planted"):
            await asyncio.wait_for(task, 10)
        assert worker.checks == 1
    assert jenv.row(job_id).state == "queued"


async def test_a_drift_met_by_a_run_stops_the_worker_and_hands_the_leases_back(
    jenv: JobsEnv,
) -> None:
    """A ``DatabaseRoleError`` on a new connection inside a run (here raised where the run would
    open one) is not the job's failure: the worker stops, no attempt counted."""
    _, refused = jenv.job()
    _, other = jenv.job()
    in_run = asyncio.Event()

    async def run(job_run: JobRun) -> Complete:
        if job_run.lease.job_id == refused:
            await until(in_run.is_set)
            raise DatabaseRoleError(DRIFT)
        in_run.set()
        await asyncio.sleep(60)  # stops at shutdown
        return Complete()

    async with running(jenv, registry_for(run)) as (_, _, task):
        with pytest.raises(DatabaseRoleError, match="planted"):
            await asyncio.wait_for(task, 30)
    for job_id in (refused, other):
        row = jenv.row(job_id)
        assert (row.state, row.attempt, row.error_category) == ("queued", 0, None)
        assert jenv.kinds(job_id)[-1] == "requeued"


def handed_back(caplog: pytest.LogCaptureFixture, job_id: uuid.UUID) -> list[str]:
    return [
        getattr(r, "job_state", "")
        for r in caplog.records
        if r.getMessage() == "job handed back" and getattr(r, "job_id", None) == str(job_id)
    ]


async def test_a_drift_met_by_a_run_while_shutdown_waits_is_handed_back_once(
    jenv: JobsEnv, caplog: pytest.LogCaptureFixture
) -> None:
    """A run raising ``DatabaseRoleError`` while ``stop_runs`` waits for it is both stranded and
    one of the waited runs: one hand-back, not a second that finds the lease gone."""
    _, job_id = jenv.job()
    in_run = asyncio.Event()

    async def run(job_run: JobRun) -> Complete:
        in_run.set()
        await until(job_run.shutdown.is_set, 30)
        raise DatabaseRoleError(DRIFT)  # its next connection, after SIGTERM

    caplog.set_level("INFO", logger="ownvitals.jobs.runner")
    async with running(jenv, registry_for(run)) as (_, stop, task):
        await asyncio.wait_for(in_run.wait(), 10)
        stop.set()
        await asyncio.wait_for(task, 30)
    assert handed_back(caplog, job_id) == ["queued"]
    row = jenv.row(job_id)
    assert (row.state, row.attempt) == ("queued", 0)
    assert jenv.kinds(job_id).count("requeued") == 1


async def test_a_drift_met_by_the_heartbeat_stops_the_worker_and_hands_the_lease_back(
    jenv: JobsEnv, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``DatabaseRoleError`` on the heartbeat's connection is not a database blip (which would
    lose the run after three): the run is abandoned and the worker stops."""
    from ownvitals.jobs import run as run_module

    _, job_id = jenv.job()
    beats = {"n": 0}
    real = run_module.fenced_session

    @contextlib.asynccontextmanager
    async def drifted(factory: Any, lease: Any) -> AsyncIterator[Any]:
        beats["n"] += 1  # the run below opens no fenced transaction: only the heartbeat does
        raise DatabaseRoleError(DRIFT)
        async with real(factory, lease) as fs:  # pragma: no cover
            yield fs

    async def run(job_run: JobRun) -> Complete:
        job_run.lease_seconds = 0.15  # read by the heartbeat, which starts after this
        await asyncio.sleep(60)  # abandoned by the heartbeat
        return Complete()

    monkeypatch.setattr(run_module, "fenced_session", drifted)
    caplog.set_level("INFO", logger="ownvitals.jobs.runner")
    async with running(jenv, registry_for(run)) as (_, _, task):
        with pytest.raises(DatabaseRoleError, match="planted"):
            await asyncio.wait_for(task, 30)
    assert beats["n"] == 1  # stopped at the first refusal, not retried as a blip
    assert handed_back(caplog, job_id) == ["queued"]
    row = jenv.row(job_id)
    assert (row.state, row.attempt, row.error_category) == ("queued", 0, None)


async def test_a_drift_met_by_maintenance_stops_the_worker(
    jenv: JobsEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def drifted(factory: object) -> int:
        raise DatabaseRoleError(DRIFT)

    monkeypatch.setattr(worker_loop.queue, "reap", drifted)
    async with running(jenv, registry_for(done_run())) as (_, _, task):
        with pytest.raises(DatabaseRoleError, match="planted"):
            await asyncio.wait_for(task, 10)


async def test_a_failing_maintenance_step_is_logged_and_the_loop_goes_on(
    jenv: JobsEnv, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(factory: object) -> int:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(worker_loop.queue, "reap", broken)
    caplog.set_level("WARNING", logger="ownvitals.worker.loop")
    _, job_id = jenv.job()
    async with running(jenv, registry_for(done_run())):
        await until(lambda: jenv.row(job_id).state == "completed")
    failures = [r for r in caplog.records if r.getMessage() == "job maintenance failed"]
    assert failures
    assert all(getattr(r, "error_type", "") == "RuntimeError" for r in failures)
    assert all("synthetic" not in r.getMessage() for r in failures)


async def test_sigterm_cancels_a_maintenance_step_in_progress(
    jenv: JobsEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow maintenance step does not hold the shutdown."""
    in_step = asyncio.Event()
    cancelled: list[bool] = []

    async def slow(factory: object) -> int:
        in_step.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return 0

    monkeypatch.setattr(worker_loop.queue, "reap", slow)
    async with running(jenv, registry_for(done_run())) as (_, stop, task):
        await asyncio.wait_for(in_step.wait(), 10)
        started = time.monotonic()
        stop.set()
        await asyncio.wait_for(task, 10)
        assert time.monotonic() - started < 2
    assert cancelled == [True]
