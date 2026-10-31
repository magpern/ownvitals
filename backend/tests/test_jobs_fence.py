"""The issuer, the fence, the heartbeat and the stale worker (ADR 0005 section 4).

Acceptance criterion 1 of #9: a lost lease is reaped, and every write the stale holder still tries
is refused by the fence. Synthetic jobs of the test-only kind."""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text

from ctx import make_context
from jobcore import (
    JobsEnv,
    KindInput,
    claim,
    jenv_fixture,  # noqa: F401 - fixtures
    registry_for,
)
from jobtools import KIND, admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.db import DatabaseRoleError, user_session
from ownvitals.jobs import (
    NO_ACCOUNTS,
    Complete,
    FencedSession,
    JobRun,
    LeaseLost,
    RunStopped,
    fenced_session,
    queue,
    transitions,
)
from ownvitals.jobs.fence import CALL_LEASE_MARGIN_SECONDS, Lease
from ownvitals.jobs.issuer import issue
from ownvitals.jobs.runner import Execution, RunConfig, execute

REMAINING = "SELECT extract(epoch FROM lease_expires_at - now()) FROM jobs WHERE id = :id"
DRIFT = "the job queue does not match ADR 0005 section 3: planted by the test"


async def _lease(env: JobsEnv, execution: Execution) -> Lease:
    return (await issue(env.factory, execution.claimed, env.accounts)).lease


def _run(env: JobsEnv, lease: Lease, lease_seconds: float = 30) -> JobRun:
    return JobRun(
        env.factory,
        lease,
        kind=KIND,
        input=KindInput(),
        attempt=1,
        lease_seconds=lease_seconds,
        shutdown=asyncio.Event(),
    )


async def test_the_issuer_builds_a_user_context_from_the_claimed_row_only(jenv: JobsEnv) -> None:
    owner, job_id = jenv.job()
    connection_id = uuid.uuid4()
    jenv.accounts.refs = frozenset({("withings", connection_id)})
    execution = await claim(jenv)
    issued = await issue(jenv.factory, execution.claimed, jenv.accounts)
    ctx = issued.lease.ctx
    assert issued.active
    assert (ctx.user_id, ctx.role) == (owner, "user")  # never admin authority
    assert ctx.connections == frozenset({("withings", connection_id)})
    assert (issued.lease.job_id, issued.lease.lease_version) == (job_id, 1)
    assert jenv.accounts.asked == [owner]


async def test_the_issuer_refuses_connection_refs_that_are_not_a_frozenset(
    jenv: JobsEnv,
) -> None:
    jenv.job()
    jenv.accounts.refs = [("withings", uuid.uuid4())]  # type: ignore[assignment]
    execution = await claim(jenv)
    with pytest.raises(TypeError, match="frozenset"):
        await issue(jenv.factory, execution.claimed, jenv.accounts)


async def test_an_inactive_owner_fails_the_job_with_account_disabled(jenv: JobsEnv) -> None:
    owner, job_id = jenv.job()
    jenv.accounts.disabled.add(owner)
    calls: list[int] = []

    async def run(job_run: JobRun) -> Complete:
        calls.append(1)
        return Complete()

    execution = await claim(jenv)
    issued = await issue(jenv.factory, execution.claimed, jenv.accounts)
    assert not issued.active
    assert issued.lease.ctx.connections == frozenset()
    await execute(execution, jenv.factory, registry_for(run), jenv.config(), asyncio.Event())
    row = jenv.row(job_id)
    assert (row.state, row.error_category) == ("failed", "account_disabled")
    assert calls == []
    assert jenv.kinds(job_id) == ["claimed", "failed"]


async def test_the_default_account_directory_fails_closed(jenv: JobsEnv) -> None:
    """Pins the seam's default until #10 and #13 (#77): every owner is inactive and has no
    connection refs, so every job fails ``account_disabled`` and its handler never runs."""
    owner, job_id = jenv.job()
    factory = jenv.factory
    assert await NO_ACCOUNTS.owner_active(factory, owner) is False
    bare = make_context(owner)
    assert await NO_ACCOUNTS.connection_refs(factory, bare) == frozenset()
    assert RunConfig().accounts is NO_ACCOUNTS
    calls: list[int] = []

    async def run(job_run: JobRun) -> Complete:
        calls.append(1)
        return Complete()

    execution = await claim(jenv)
    await execute(execution, factory, registry_for(run), RunConfig(), asyncio.Event())
    row = jenv.row(job_id)
    assert (row.state, row.error_category) == ("failed", "account_disabled")
    assert calls == []


async def test_the_fence_refuses_a_wrong_token_job_or_owner_and_rolls_back(
    jenv: JobsEnv,
) -> None:
    _, job_id = jenv.job()
    lease = await _lease(jenv, await claim(jenv))
    async with fenced_session(jenv.factory, lease) as fs:
        seq = await fs.event("progress", {"done": 1, "total": 3})
    assert seq == 2  # claimed was 1
    stale = Lease(lease.job_id, lease.lease_version - 1, lease.ctx)
    ahead = Lease(lease.job_id, lease.lease_version + 1, lease.ctx)
    unknown = Lease(uuid.uuid4(), lease.lease_version, lease.ctx)
    someone_else = Lease(lease.job_id, lease.lease_version, make_context(uuid.uuid4()))
    for wrong in (stale, ahead, unknown, someone_else):
        with pytest.raises(LeaseLost):
            async with fenced_session(jenv.factory, wrong):
                pass

    async def write_then_lose() -> None:
        async with fenced_session(jenv.factory, lease) as fs:
            await fs.event("progress", {"done": 2, "total": 3})
            raise LeaseLost  # what a later fenced statement raises: everything rolls back

    with pytest.raises(LeaseLost):
        await write_then_lose()
    assert jenv.kinds(job_id) == ["claimed", "progress"]
    assert jenv.row(job_id).next_seq == 2
    assert lease.owner_id == jenv.row(job_id).owner_id


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("nope", {}),
        ("progress", {"done": 2, "total": 1}),
        ("progress", {"done": 1, "total": 2, "value": 72.5}),
        ("progress", {"done": -1, "total": 2}),
        ("failed", {"category": "made_up"}),
        ("failed", {"category": "internal", "detail": "text"}),
        ("completed", {"x": {"nested": 1}}),
    ],
)
async def test_event_payloads_match_their_model_and_kinds_are_known(
    jenv: JobsEnv, kind: str, payload: dict[str, Any]
) -> None:
    _, job_id = jenv.job()
    lease = await _lease(jenv, await claim(jenv))
    with pytest.raises(ValueError, match="event"):
        async with fenced_session(jenv.factory, lease) as fs:
            await fs.event(kind, payload)
    assert jenv.kinds(job_id) == ["claimed"]


Write = Callable[[FencedSession], Awaitable[object]]
# Every write a stale holder could try, each of which must be refused. The ones marked True
# carry the fence in their own statement, so they are refused even past a passing fence.
STALE_WRITES: dict[str, tuple[Write, bool]] = {
    "event": (lambda fs: fs.event("progress", {"done": 1, "total": 2}), True),
    "complete": (transitions.complete, True),
    "fail": (lambda fs: transitions.fail(fs, "internal"), True),
    "cancel": (transitions.cancel, True),
    "retry": (lambda fs: transitions.retry(fs, "upstream_unavailable", 0), True),
    "hand_back": (transitions.hand_back, True),
    "attempts": (transitions.attempts, True),
    "heartbeat": (lambda fs: fs.extend_lease(600, heartbeat=True), True),
    "provider_call": (lambda fs: fs.extend_lease_for_call(300), True),
    "own_write": (lambda fs: fs.db.execute(text("SELECT 1")), False),
}


async def test_a_stale_worker_writes_nothing_after_the_reaper_and_b_completes(
    jenv: JobsEnv,
) -> None:
    """Acceptance 1: A's lease expires, the reaper requeues the job (bumping the token), B claims
    it; nothing A still tries lands, A's heartbeat and checkpoint stop A, and B completes."""
    _, job_id = jenv.job()
    lease_a = await _lease(jenv, await claim(jenv, "worker-a"))
    async with fenced_session(jenv.factory, lease_a) as fs:
        await fs.event("progress", {"done": 1, "total": 4})
    jenv.expire_lease(job_id)
    assert await queue.reap(jenv.factory) == 1
    reaped = jenv.row(job_id)
    assert (reaped.state, reaped.lease_version) == ("queued", 2)
    jenv.sql("UPDATE jobs SET available_at = now() WHERE id = :id", id=job_id)
    execution_b = await claim(jenv, "worker-b")
    lease_b = await _lease(jenv, execution_b)
    assert lease_b.lease_version == 3  # claim 1, reap 2, claim 3
    before = (jenv.row(job_id), jenv.events(job_id))

    for name, (write, self_fenced) in STALE_WRITES.items():
        with pytest.raises(LeaseLost):
            async with fenced_session(jenv.factory, lease_a) as fs:
                await write(fs)
        if self_fenced:  # past the fence too: the statement itself carries it
            with pytest.raises(LeaseLost):
                async with user_session(jenv.factory, lease_a.ctx) as db:
                    await write(FencedSession(db, lease_a, False))
        assert (jenv.row(job_id), jenv.events(job_id)) == before, name

    run_a = _run(jenv, lease_a, lease_seconds=0.03)
    await asyncio.wait_for(run_a.heartbeat(), timeout=5)  # returns once the fence fails
    assert run_a.lost
    for stop in (run_a.checkpoint(), run_a.progress(2, 4)):
        with pytest.raises(RunStopped) as stopped:
            await stop
        assert stopped.value.reason == "lost"
    with pytest.raises(RunStopped):
        async with run_a.provider_call(10):
            pytest.fail("a lost run must not start a provider call")
    assert (jenv.row(job_id), jenv.events(job_id)) == before

    async def finish(job_run: JobRun) -> Complete:
        await job_run.progress(4, 4)
        return Complete()

    await execute(execution_b, jenv.factory, registry_for(finish), jenv.config(), asyncio.Event())
    done = jenv.row(job_id)
    assert (done.state, done.lease_version, done.leased_by) == ("completed", 3, None)
    assert [(kind, v) for _, kind, _, v in jenv.events(job_id)] == [
        ("claimed", 1),
        ("progress", 1),
        ("retry_scheduled", 2),
        ("claimed", 3),
        ("progress", 3),
        ("completed", 3),
    ]


async def test_a_stale_runner_records_nothing_after_the_reap(jenv: JobsEnv) -> None:
    """The whole runner path: A's handler is past its last checkpoint when the reaper takes the
    job; A's final fenced transaction fails, so A records nothing and leaves the job to B."""
    _, job_id = jenv.job()
    reaped = asyncio.Event()

    async def run(job_run: JobRun) -> Complete:
        jenv.expire_lease(job_id)
        assert await queue.reap(jenv.factory) == 1
        reaped.set()

        async def store(fs: FencedSession) -> None:
            pytest.fail("a stale store must not run")

        return Complete(store)

    execution = await claim(jenv, "worker-a")
    await execute(execution, jenv.factory, registry_for(run), jenv.config(), asyncio.Event())
    assert reaped.is_set()
    assert (execution.result, execution.settled) == ("lost", True)
    row = jenv.row(job_id)
    assert (row.state, row.lease_version) == ("queued", 2)
    assert jenv.kinds(job_id) == ["claimed", "retry_scheduled"]


async def test_the_heartbeat_extends_with_greatest_and_sees_a_cancel(jenv: JobsEnv) -> None:
    _, job_id = jenv.job()
    lease = await _lease(jenv, await claim(jenv))
    async with fenced_session(jenv.factory, lease) as fs:
        await fs.extend_lease_for_call(500)  # a provider call's deadline
    assert jenv.sql(REMAINING, id=job_id).scalar_one() > 500 + CALL_LEASE_MARGIN_SECONDS - 5
    async with fenced_session(jenv.factory, lease) as fs:
        assert await fs.extend_lease(60, heartbeat=True) is False
    assert jenv.sql(REMAINING, id=job_id).scalar_one() > 500  # not shortened
    jenv.sql("UPDATE jobs SET cancel_requested_at = now() WHERE id = :id", id=job_id)
    run = _run(jenv, lease, lease_seconds=0.06)
    task = asyncio.ensure_future(run.heartbeat())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if run.cancel_requested:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert run.cancel_requested
    assert not run.lost
    assert jenv.sql(REMAINING, id=job_id).scalar_one() > 500


@pytest.mark.parametrize("seconds", [0, -1])
async def test_a_lease_extension_needs_a_positive_time(jenv: JobsEnv, seconds: float) -> None:
    jenv.job()
    lease = await _lease(jenv, await claim(jenv))
    async with fenced_session(jenv.factory, lease) as fs:
        with pytest.raises(ValueError, match="positive"):
            await fs.extend_lease(seconds)
        with pytest.raises(ValueError, match="positive"):
            await fs.extend_lease_for_call(seconds)
    with pytest.raises(ValueError, match="positive"):
        async with _run(jenv, lease).provider_call(seconds):
            pass


async def test_a_provider_call_extends_the_lease_and_commits_before_the_call(
    jenv: JobsEnv,
) -> None:
    """No transaction is open across a provider call (ADR 0005 section 4): inside the call another
    connection can take the job row's lock, and the lease covers the deadline plus 30 s."""
    _, job_id = jenv.job()
    lease = await _lease(jenv, await claim(jenv))
    run = _run(jenv, lease)
    async with run.provider_call(200):
        assert run.in_flight == 1
        remaining = jenv.sql(REMAINING, id=job_id).scalar_one()
        assert 200 + CALL_LEASE_MARGIN_SECONDS - 5 < remaining <= 200 + CALL_LEASE_MARGIN_SECONDS
        jenv.sql("SELECT 1 FROM jobs WHERE id = :id FOR UPDATE NOWAIT", id=job_id)
    assert run.in_flight == 0


async def test_a_lost_lease_abandons_the_run_but_not_a_call_in_flight(jenv: JobsEnv) -> None:
    """Deterministic: the run is marked lost (what the heartbeat does on 0 rows; the heartbeat
    itself is covered by the stale-worker test) while a provider call is in flight."""
    _, job_id = jenv.job()
    reached: list[str] = []

    async def run(job_run: JobRun) -> Complete:
        async with job_run.provider_call(5):
            jenv.sql("UPDATE jobs SET lease_version = lease_version + 1 WHERE id = :id", id=job_id)
            job_run.mark_lost()
            await asyncio.sleep(0.2)  # still in the call: not abandoned
            reached.append("call returned")
        await asyncio.sleep(60)  # abandoned here, once the call is over
        reached.append("after")
        return Complete()

    execution = await claim(jenv)
    started = time.monotonic()
    await asyncio.wait_for(
        execute(execution, jenv.factory, registry_for(run), jenv.config(), asyncio.Event()), 10
    )
    # Far below the handler's sleep: only the on_lost cancel can end the run this early.
    assert time.monotonic() - started < 5
    assert reached == ["call returned"]
    assert (execution.result, execution.settled) == ("lost", True)
    assert jenv.row(job_id).state == "running"  # the reaper's, once the lease expires


async def test_a_role_error_met_by_the_heartbeat_waits_for_a_call_in_flight_then_stops(
    jenv: JobsEnv,
) -> None:
    """``mark_fatal`` (what the heartbeat does on ``DatabaseRoleError``) during a provider call
    does not abandon the call; once it is over the run is abandoned and the runner raises the
    error, with nothing recorded (the worker hands the lease back)."""
    _, job_id = jenv.job()
    reached: list[str] = []

    async def run(job_run: JobRun) -> Complete:
        async with job_run.provider_call(5):
            job_run.mark_fatal(DatabaseRoleError(DRIFT))
            await asyncio.sleep(0.2)  # still in the call: not abandoned
            reached.append("call returned")
        await asyncio.sleep(60)  # abandoned here, once the call is over
        reached.append("after")
        return Complete()

    execution = await claim(jenv)
    started = time.monotonic()
    with pytest.raises(DatabaseRoleError, match="planted"):
        await asyncio.wait_for(
            execute(execution, jenv.factory, registry_for(run), jenv.config(), asyncio.Event()),
            10,
        )
    assert time.monotonic() - started < 5  # only the abandon can end the run this early
    assert reached == ["call returned"]
    assert execution.needs_hand_back()
    assert (jenv.row(job_id).state, jenv.row(job_id).attempt) == ("running", 1)


async def test_a_checkpoint_after_a_role_error_raises_it_without_a_transaction(
    jenv: JobsEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ownvitals.jobs import run as run_module

    jenv.job()
    lease = await _lease(jenv, await claim(jenv))
    run = _run(jenv, lease)
    opened: list[object] = []
    real = run_module.fenced_session

    def counting(*args: Any) -> Any:
        opened.append(args)
        return real(*args)

    monkeypatch.setattr(run_module, "fenced_session", counting)
    run.mark_fatal(DatabaseRoleError(DRIFT))
    for stop in (run.checkpoint(), run.progress(1, 2)):
        with pytest.raises(DatabaseRoleError, match="planted"):
            await stop
    assert opened == []


async def test_a_stale_write_racing_a_concurrent_reaper_never_lands_after_it(
    jenv: JobsEnv,
) -> None:
    """A real race on separate connections: A's fenced write and the reaper start together.
    Either A's write commits first (the reaper skips the locked row and reaps it next time) or
    the reaper bumps the token first and A gets ``LeaseLost``; never an A event after the reap."""
    outcomes: set[str] = set()
    for _ in range(20):
        _, job_id = jenv.job()
        lease = await _lease(jenv, await claim(jenv, "worker-a"))
        jenv.expire_lease(job_id)
        go = asyncio.Event()

        async def write_a(lease: Lease = lease, go: asyncio.Event = go) -> str:
            await go.wait()
            try:
                async with fenced_session(jenv.factory, lease) as fs:
                    await fs.event("progress", {"done": 1, "total": 2})
                    await asyncio.sleep(0.01)  # hold the row lock a moment
            except LeaseLost:
                return "lost"
            return "written"

        async def reap(go: asyncio.Event = go) -> int:
            await go.wait()
            return await queue.reap(jenv.factory)

        tasks = asyncio.gather(write_a(), reap())
        go.set()
        outcome, reaped = await tasks
        outcomes.add(outcome)
        if not reaped:
            assert await queue.reap(jenv.factory) == 1  # the lock is gone now
        events = jenv.events(job_id)
        reap_seq = next(seq for seq, kind, _, _ in events if kind == "retry_scheduled")
        assert all(seq < reap_seq for seq, _, _, v in events if v == lease.lease_version)
        with pytest.raises(LeaseLost):
            async with fenced_session(jenv.factory, lease):
                pass
        assert jenv.row(job_id).state == "queued"
    assert outcomes  # both orders are fine; each run checked the invariant


class _Undo(Exception):
    """Rolls a test transaction back."""


async def test_each_transition_statement_carries_its_own_fence(jenv: JobsEnv) -> None:
    """Every statement of ``transitions`` matches nothing for a stale token or a job no longer
    ``running``, even inside a transaction whose fence passed (each condition mutated)."""
    _, job_id = jenv.job()
    lease = await _lease(jenv, await claim(jenv))
    async with fenced_session(jenv.factory, lease) as fs:
        assert (await transitions.attempts(fs)).attempt == 1  # the real lease passes

    async def stale_writes() -> None:
        async with fenced_session(jenv.factory, lease) as fs:
            for version in (lease.lease_version - 1, lease.lease_version + 1):
                stale = FencedSession(fs.db, Lease(job_id, version, lease.ctx), False)
                for write, self_fenced in STALE_WRITES.values():
                    if self_fenced:
                        with pytest.raises(LeaseLost):
                            await write(stale)
            await fs.db.execute(
                text(
                    "UPDATE jobs SET state = 'queued', leased_by = NULL, lease_expires_at = NULL, "
                    "heartbeat_at = NULL WHERE id = :id"
                ),
                {"id": job_id},
            )
            for write, self_fenced in STALE_WRITES.values():
                if self_fenced:
                    with pytest.raises(LeaseLost):
                        await write(fs)
            row = (
                await fs.db.execute(
                    text("SELECT next_seq, lease_version FROM jobs WHERE id = :id"), {"id": job_id}
                )
            ).one()
            assert tuple(row) == (1, 1)  # no statement changed the row
            raise _Undo

    with pytest.raises(_Undo):
        await stale_writes()
    row = jenv.row(job_id)
    assert (row.state, row.next_seq) == ("running", 1)
    assert jenv.kinds(job_id) == ["claimed"]
