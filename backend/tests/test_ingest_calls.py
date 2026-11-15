"""A refused rate-limit claim is waited out within the attempt (#349, #294). Synthetic ids only.

The unit tests script the limiter's answers; the database tests use the real limiter
(``docker/postgres/withings/rate.sql``) with its four slots held by a synthetic account.
"""

import asyncio
import functools
import json
import re
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ctx import make_context
from jobtools import engine_for, ensure_user
from ownvitals.config import Settings
from ownvitals.db import create_engine, create_session_factory
from ownvitals.ingest import calls
from ownvitals.ingest.calls import CLAIM_WAIT_SECONDS, Account, limited
from ownvitals.jobs.errors import JobFailure, RunStopped
from ownvitals.jobs.fence import Lease
from ownvitals.jobs.run import JobRun
from ownvitals.jobs.withings import ConnectInput
from ownvitals.withings import rate
from ownvitals.withings.errors import Transient
from withingsschema import new_app

ACCOUNT = Account(uuid.uuid4(), 7, ("user.metrics",), "UTC", "synthetic-token")
REPO = Path(__file__).resolve().parents[2]
GRANTED = rate.Claim(granted=True, lease_id=None, retry_at=None, reason="granted")
CALLS = 3  # Withings calls per backfill job in the fan-out test


def _refused(reason: str, after: float = 0.2) -> rate.Claim:
    return rate.Claim(
        granted=False,
        lease_id=None,
        retry_at=datetime.now(UTC) + timedelta(seconds=after),
        reason=reason,
    )


class _Sync:
    def connection(self) -> object:
        return object()


class _Db:
    async def run_sync(self, fn: Any) -> Any:
        return fn(_Sync())


class _Fs:
    db = _Db()


class _Job:
    """The parts of ``JobRun`` that ``_take`` uses; a cancel arrives after ``cancel_after``."""

    def __init__(self, cancel_after: int | None = None) -> None:
        self.cancel_requested = False
        self.shutdown = asyncio.Event()
        self.sessions = 0
        self.checkpoints = 0
        self._cancel_after = cancel_after

    @asynccontextmanager
    async def fenced(self) -> AsyncIterator[_Fs]:
        self.sessions += 1
        yield _Fs()
        if self._cancel_after is not None and self.sessions >= self._cancel_after:
            self.cancel_requested = True

    async def checkpoint(self) -> None:
        self.checkpoints += 1
        if self.cancel_requested:
            raise RunStopped("cancelled")
        if self.shutdown.is_set():
            raise RunStopped("shutdown")


def _script(monkeypatch: pytest.MonkeyPatch, answers: list[rate.Claim]) -> list[int]:
    lanes: list[int] = []

    def claim(_conn: object, _app: int, _account: uuid.UUID, lane: int) -> rate.Claim:
        lanes.append(lane)
        return answers.pop(0)

    monkeypatch.setattr(calls.rate, "claim", claim)
    return lanes


def _NONE() -> float:
    return 1.0  # no jitter


class _Clock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


async def test_a_queue_refusal_is_asked_again_within_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lanes = _script(
        monkeypatch, [_refused("busy"), _refused("wait"), _refused("empty", 0.6), GRANTED]
    )
    job, clock = _Job(), _Clock()
    got = await calls._take(
        cast(JobRun, job), ACCOUNT, rate.LANE_BACKFILL, sleep=clock, jitter=_NONE
    )
    assert got is GRANTED
    assert lanes == [rate.LANE_BACKFILL] * 4  # never a higher lane than asked
    assert len(clock.slept) == 3
    assert clock.slept == [pytest.approx(0.2), pytest.approx(0.4), pytest.approx(0.8)]
    assert job.checkpoints == 0  # no extra transaction while nothing asks the run to stop


async def test_each_wait_is_floored_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    gone = rate.Claim(granted=False, lease_id=None, retry_at=None, reason="busy")
    _script(monkeypatch, [_refused("busy", -5), _refused("empty", 3600), gone, GRANTED])
    clock = _Clock()
    await calls._take(cast(JobRun, _Job()), ACCOUNT, rate.LANE_BACKFILL, sleep=clock, jitter=_NONE)
    assert clock.slept[0] == pytest.approx(0.2)
    assert clock.slept[1] == pytest.approx(5.0)
    assert clock.slept[2] == pytest.approx(5.0)


def test_the_longest_wait_stays_inside_the_window_a_waiting_lane_stays_live() -> None:
    """``rate.sql`` keeps a waiting lane live for 10 s after its last ask; no wait of ``_take``,
    jitter included, may reach that, or the lane drops out between its asks."""
    sql = (REPO / "docker" / "postgres" / "withings" / "rate.sql").read_text(encoding="utf-8")
    windows = set(re.findall(r"seen_at >= v_now - interval '(\d+) seconds'", sql))
    assert windows == {"10"}
    longest = max(
        calls._step(datetime.now(UTC) + timedelta(hours=1), asked, calls._JITTER_MAX)
        for asked in range(9)
    )
    assert longest == calls._STEP_CAP
    assert longest * 2 <= int(windows.pop())


async def test_waits_back_off_and_are_jittered(monkeypatch: pytest.MonkeyPatch) -> None:
    _script(monkeypatch, [_refused("busy") for _ in range(7)] + [GRANTED])
    clock = _Clock()
    await calls._take(
        cast(JobRun, _Job()), ACCOUNT, rate.LANE_BACKFILL, sleep=clock, jitter=lambda: 1.0
    )
    assert clock.slept == [pytest.approx(s) for s in (0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0)]
    _script(monkeypatch, [_refused("busy"), GRANTED])
    jittered = _Clock()
    await calls._take(
        cast(JobRun, _Job()), ACCOUNT, rate.LANE_BACKFILL, sleep=jittered, jitter=lambda: 1.25
    )
    assert jittered.slept == [pytest.approx(0.25)]  # stretched, never below the limiter's retry_at


async def test_the_wait_is_bounded_and_then_fails_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script(monkeypatch, [_refused("busy", 1.0) for _ in range(1000)])
    clock = _Clock()
    with pytest.raises(JobFailure) as raised:
        await calls._take(
            cast(JobRun, _Job()), ACCOUNT, rate.LANE_BACKFILL, sleep=clock, jitter=_NONE
        )
    assert raised.value.category == "rate_limited"
    assert raised.value.retry_after is not None
    assert sum(clock.slept) <= CLAIM_WAIT_SECONDS
    assert sum(clock.slept) > CLAIM_WAIT_SECONDS - 5.0


async def test_a_601_hold_fails_the_attempt_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _script(monkeypatch, [_refused("paused", 300.0), GRANTED])
    clock = _Clock()
    with pytest.raises(JobFailure) as raised:
        await calls._take(
            cast(JobRun, _Job()), ACCOUNT, rate.LANE_BACKFILL, sleep=clock, jitter=_NONE
        )
    assert raised.value.category == "rate_limited"
    assert raised.value.retry_after == pytest.approx(300.0, abs=2.0)
    assert clock.slept == []


@pytest.mark.parametrize("stop", ["cancelled", "shutdown"])
async def test_a_cancel_or_a_shutdown_stops_the_wait(
    monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    _script(monkeypatch, [_refused("busy") for _ in range(10)] + [GRANTED])
    job, clock = _Job(cancel_after=2 if stop == "cancelled" else None), _Clock()
    if stop == "shutdown":
        job.shutdown.set()
    with pytest.raises(RunStopped) as raised:
        await calls._take(cast(JobRun, job), ACCOUNT, rate.LANE_BACKFILL, sleep=clock, jitter=_NONE)
    assert raised.value.reason == stop
    assert len(clock.slept) == (1 if stop == "cancelled" else 0)


class _CallJob(_Job):
    @asynccontextmanager
    async def provider_call(self, _deadline: float) -> AsyncIterator[None]:
        yield


async def test_a_genuine_failure_still_fails_the_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal is waited out, but the call's own failure is the attempt's, as before."""
    _script(monkeypatch, [_refused("busy"), GRANTED])
    monkeypatch.setattr(calls, "_take", functools.partial(calls._take, sleep=_Clock()))
    made = 0

    async def call() -> None:
        nonlocal made
        made += 1
        raise Transient("synthetic")

    with pytest.raises(JobFailure) as raised:
        await limited(cast(JobRun, _CallJob()), ACCOUNT, rate.LANE_BACKFILL, call)
    assert raised.value.category == "upstream_unavailable"
    assert made == 1


# --- the real limiter --------------------------------------------------------------------


@pytest.fixture
async def sessions(
    app_database_url: str, schema: None
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(Settings(database_url=app_database_url))  # pyright: ignore[reportArgumentType]
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def _seed(url: str, budget: int) -> int:
    app_id = new_app(url)
    with engine_for(url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO withings_rate_buckets (app_id, remaining, budget, refilled_at) "
                "VALUES (:app, :budget, :budget, clock_timestamp())"
            ),
            {"app": app_id, "budget": budget},
        )
    return app_id


def _remaining(url: str, app_id: int) -> float:
    with engine_for(url) as engine, engine.connect() as conn:
        return float(
            conn.execute(
                text("SELECT remaining FROM withings_rate_buckets WHERE app_id = :app"),
                {"app": app_id},
            ).scalar_one()
        )


def _job(
    database_url: str,
    app_id: int,
    factory: async_sessionmaker[AsyncSession],
    *,
    of: Account | None = None,
    owner: uuid.UUID | None = None,
) -> tuple[JobRun, Account]:
    """A running backfill on its last attempt (3 of 3) for a new synthetic connection.

    With ``of`` (and the ``owner`` it was made with) it is one more backfill of that connection."""
    job_id = uuid.uuid4()
    owner = owner or uuid.uuid4()
    connection = of.connection_id if of is not None else uuid.uuid4()
    with engine_for(database_url) as engine, engine.begin() as conn:
        if of is None:
            ensure_user(conn, owner)
            conn.execute(
                text(
                    "INSERT INTO connections (id, owner_id, provider, status, public) "
                    "VALUES (:id, :owner, 'withings', 'needs_reconnect', CAST(:public AS jsonb))"
                ),
                {
                    "id": connection,
                    "owner": owner,
                    "public": json.dumps({"app_id": app_id, "scopes": ["user.metrics"]}),
                },
            )
        conn.execute(
            text(
                "INSERT INTO jobs (id, owner_id, kind, state, input, origin, lane, leased_by, "
                "lease_expires_at, heartbeat_at, lease_version, attempt, max_attempts) "
                "VALUES (:id, :owner, 'withings_backfill', 'running', CAST(:input AS jsonb), "
                "'scheduler', 5, 'synthetic-worker', now() + interval '30 minutes', now(), 1, "
                "3, 3)"
            ),
            {"id": job_id, "owner": owner, "input": json.dumps({"connection_id": str(connection)})},
        )
    run = JobRun(
        factory,
        Lease(job_id, 1, make_context(owner)),
        kind="withings_backfill",
        input=ConnectInput(connection_id=connection),
        attempt=3,
        lease_seconds=120,
        shutdown=asyncio.Event(),
    )
    return run, Account(connection, app_id, ("user.metrics",), "UTC", "synthetic-token")


async def test_held_slots_are_waited_out_and_spend_one_token(
    sessions: async_sessionmaker[AsyncSession], database_url: str, app_database_url: str
) -> None:
    """Four slots held by another account: the backfill's last attempt waits, then gets one.

    Every refused claim spends nothing, so the bucket moves by the one granted token only."""
    app_id = _seed(database_url, budget=8)
    job, account = _job(database_url, app_id, sessions)
    held: list[uuid.UUID] = []
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        for _ in range(rate.DEFAULT_SLOT_CAP):
            got = rate.claim(conn, app_id, uuid.uuid4(), rate.LANE_BACKFILL, lease_seconds=60)
            assert got.granted
            assert got.lease_id is not None
            held.append(got.lease_id)
    before = _remaining(database_url, app_id)
    asked: list[float] = []

    async def sleep(seconds: float) -> None:
        asked.append(seconds)
        if len(asked) == 3:  # another call ends: its slot is free again
            with engine_for(app_database_url) as engine, engine.begin() as conn:
                rate.release(conn, held.pop())

    got = await calls._take(job, account, rate.LANE_BACKFILL, sleep=sleep)
    assert got.granted
    assert got.lease_id is not None
    assert len(asked) >= 3
    after = _remaining(database_url, app_id)
    assert before - 1.5 < after < before - 0.5  # one token, plus at most a little refill
    await calls._release(job, got)


async def test_a_last_attempt_that_cannot_get_a_slot_fails_rate_limited(
    sessions: async_sessionmaker[AsyncSession], database_url: str, app_database_url: str
) -> None:
    """The wait has its bound: after it the attempt fails as before, and spends no token."""
    app_id = _seed(database_url, budget=8)
    job, account = _job(database_url, app_id, sessions)
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        for _ in range(rate.DEFAULT_SLOT_CAP):
            assert rate.claim(
                conn, app_id, uuid.uuid4(), rate.LANE_BACKFILL, lease_seconds=60
            ).granted
    before = _remaining(database_url, app_id)
    asked: list[float] = []

    async def sleep(seconds: float) -> None:
        asked.append(seconds)

    with pytest.raises(JobFailure) as raised:
        await calls._take(job, account, rate.LANE_BACKFILL, sleep=sleep, budget=1.0)
    assert raised.value.category == "rate_limited"
    assert 1 <= len(asked) <= 5
    assert _remaining(database_url, app_id) >= before


async def test_a_backfill_fan_out_on_one_account_spends_no_attempt_on_the_limiter(
    sessions: async_sessionmaker[AsyncSession], database_url: str, app_database_url: str
) -> None:
    """#294: seven backfills of one connection at once, each on its last attempt (3 of 3).

    They compete for the four slots through the real limiter, so some claims are refused; every
    refusal is waited out and no job fails. Each call spends exactly one granted token."""
    app_id = _seed(database_url, budget=100)
    owner = uuid.uuid4()
    first, account = _job(database_url, app_id, sessions, owner=owner)
    jobs = [first] + [
        _job(database_url, app_id, sessions, of=account, owner=owner)[0] for _ in range(6)
    ]
    reasons: Counter[str] = Counter()
    real = calls.rate.claim

    def claim(*args: Any, **kwargs: Any) -> rate.Claim:
        got = real(*args, **kwargs)
        reasons[got.reason] += 1
        return got

    async def call() -> str:
        await asyncio.sleep(0.05)  # the slot is held while the call is in flight
        return "synthetic"

    async def backfill(job: JobRun) -> list[str]:
        return [await limited(job, account, rate.LANE_BACKFILL, call) for _ in range(CALLS)]

    before = _remaining(database_url, app_id)
    started = time.monotonic()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(calls.rate, "claim", claim)
        done = await asyncio.gather(*(backfill(job) for job in jobs), return_exceptions=True)
    elapsed = time.monotonic() - started
    assert done == [["synthetic"] * CALLS] * len(jobs)  # no JobFailure("rate_limited")
    assert reasons["granted"] == CALLS * len(jobs)  # one token per call, none for a refusal
    # One account on one lane is always the limiter's winner, so this fan-out sees ``busy`` (the
    # four slots) and never ``wait``; a lane or another account's turn is waited out the same way
    # (test_a_queue_refusal_is_asked_again_within_the_attempt).
    assert reasons["busy"] > 0  # the fan-out did hit the limiter
    assert set(reasons) <= {"granted", "busy"}
    spent = before - _remaining(database_url, app_id)
    refill = elapsed * 100 / 60
    assert CALLS * len(jobs) - refill - 0.5 <= spent <= CALLS * len(jobs) + 0.5


async def test_a_wait_is_never_below_the_limiters_retry_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script(monkeypatch, [_refused("busy", 3.0), GRANTED])
    clock = _Clock()
    await calls._take(cast(JobRun, _Job()), ACCOUNT, rate.LANE_BACKFILL, sleep=clock, jitter=_NONE)
    assert clock.slept[0] == pytest.approx(3.0, abs=0.2)


async def test_the_default_jitter_only_stretches_a_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    _script(monkeypatch, [_refused("busy"), GRANTED])
    bounds: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        bounds.append((low, high))
        return high

    monkeypatch.setattr(calls.random, "uniform", uniform)
    clock = _Clock()
    await calls._take(cast(JobRun, _Job()), ACCOUNT, rate.LANE_BACKFILL, sleep=clock)
    assert bounds == [(1.0, 1.25)]
    assert clock.slept[0] > 0.2
