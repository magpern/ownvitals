"""Load test of the Withings app rate limiter (#462; PLAN section 4.4, ADR 0005 section 8).

N worker threads, each with its own app-role connection, share M synthetic accounts and drive
the real Postgres functions (``withings_rate_claim``, ``_release``, ``_pause``) the way
``ownvitals.ingest.calls.limited`` does: claim, call the fake Withings, read the answer with
``interpret``, pause the app on a 601, release the slot. Assertions are on admission bounds,
never on timing:

- no over-admission: the grants never pass the bucket's budget plus what its refill can have
  added in the run (Postgres clock), and no token is lost to a race (grants plus what is left
  is at most what the bucket was given);
- no under-admission: a drained bucket admitted at least its whole budget;
- the in-flight cap of 4 slots holds;
- no starvation: round-robin gives every account a share;
- the 601 hold: no claim that starts after the pause committed is granted, until the pause ends;
  a 429 retry spends a new token like any other call.

The bucket budget is lowered by the superuser (the app role cannot write the table), so the CI
variant drains in a few seconds. Synthetic apps and account ids only; no health data.
"""

import math
import socket
import threading
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from loadtools import FakeWithings, InFlight, Profile, Report, profiles, record, resolve
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from jobtools import engine_for
from netguard import NetworkBlocked
from ownvitals.withings.errors import RateLimited, Transient, interpret
from ownvitals.withings.models import EmptyBody
from ownvitals.withings.rate import LANE_POLL, claim, pause, release
from withingsschema import new_app

# Pinned here, not read from ``rate``: a loosened constant must fail this test.
SLOT_CAP = 4
# Seconds a granted call holds its slot.
HOLD = 0.002
# A refused claim asks again after this (the job path waits for ``retry_at``; the load wants
# contention, so it asks sooner).
RETRY = 0.001
# ``remaining`` is numeric(12, 4): every claim stores it rounded to 4 places.
ROUNDING = 0.00005


def _seed(url: str, budget: int, accounts: Sequence[uuid.UUID]) -> tuple[int, datetime]:
    """A synthetic app with a full bucket of ``budget``, and every account already waiting on
    the poll lane, so the round-robin sees them all from the first claim. Returns the app and
    the bucket's ``refilled_at`` (the start of the refill bound)."""
    app_id = new_app(url)
    with engine_for(url) as engine, engine.begin() as conn:
        started = conn.execute(
            text(
                "INSERT INTO withings_rate_buckets (app_id, remaining, budget, refilled_at) "
                "VALUES (:app, :budget, :budget, clock_timestamp()) RETURNING refilled_at"
            ),
            {"app": app_id, "budget": budget},
        ).scalar_one()
        for account in accounts:
            conn.execute(
                text(
                    "INSERT INTO withings_rate_demand (app_id, account_id, lane, seen_at, "
                    "window_grants, window_started) "
                    "VALUES (:app, :account, :lane, clock_timestamp(), 0, clock_timestamp())"
                ),
                {"app": app_id, "account": account, "lane": LANE_POLL},
            )
    return app_id, started


def _bucket(url: str, app_id: int) -> tuple[float, float]:
    """The bucket's ``remaining`` and the Postgres clock now (epoch seconds)."""
    with engine_for(url) as engine, engine.connect() as conn:
        remaining, now = conn.execute(
            text(
                "SELECT remaining, extract(epoch FROM clock_timestamp()) "
                "FROM withings_rate_buckets WHERE app_id = :app"
            ),
            {"app": app_id},
        ).one()
    return float(remaining), float(now)


@dataclass
class Run:
    """What one concurrent run saw. Shared by the workers under ``lock``."""

    grants: Counter[uuid.UUID] = field(default_factory=lambda: Counter[uuid.UUID]())
    claims: int = 0
    refused: int = 0
    waits: list[float] = field(default_factory=lambda: list[float]())
    retries_429: int = 0
    busy: int = 0  # refused because every slot was taken
    paused_claims: int = 0
    paused_retry_at: set[datetime] = field(default_factory=lambda: set[datetime]())
    late_grants: int = 0  # granted although the claim started after the pause committed
    pause_done: float | None = None  # time.monotonic() when the 601 pause committed
    drained: bool = False
    errors: list[BaseException] = field(default_factory=lambda: list[BaseException]())
    in_flight: InFlight = field(default_factory=InFlight)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _drive(
    app_url: str,
    app_id: int,
    accounts: Sequence[uuid.UUID],
    workers: int,
    fake: FakeWithings,
    deadline: float,
    *,
    stop_after_paused: int | None = None,
    open_gate_on_busy: bool = False,
) -> Run:
    """``workers`` threads claim until the bucket is empty (or, with ``stop_after_paused``,
    until each has been refused for the pause that many times)."""
    run = Run()
    stop = threading.Event()
    start = threading.Barrier(workers)
    ends = time.monotonic() + deadline

    def worker(index: int) -> None:
        account = accounts[index % len(accounts)]
        engine = create_engine(app_url, pool_size=1, max_overflow=0)
        paused_seen, asked_since = 0, None
        try:
            with engine.connect() as conn:
                start.wait(timeout=deadline)
                while not stop.is_set():
                    if time.monotonic() > ends:
                        raise TimeoutError("the load run passed its deadline")
                    began = time.monotonic()
                    asked_since = began if asked_since is None else asked_since
                    with conn.begin():
                        got = claim(conn, app_id, account, LANE_POLL)
                    if got.granted and got.lease_id is not None:
                        _call(conn, got.lease_id, account, began, asked_since)
                        asked_since = None
                        continue
                    with run.lock:
                        run.claims += 1
                        run.refused += 1
                    if got.reason == "empty":
                        with run.lock:
                            run.drained = True
                        stop.set()
                    elif got.reason == "paused":
                        paused_seen += 1
                        with run.lock:
                            run.paused_claims += 1
                            if got.retry_at is not None:
                                run.paused_retry_at.add(got.retry_at)
                        if stop_after_paused is not None and paused_seen >= stop_after_paused:
                            return
                        time.sleep(RETRY)
                    elif got.reason in {"busy", "wait"}:
                        if got.reason == "busy":
                            with run.lock:
                                run.busy += 1
                            if open_gate_on_busy:
                                fake.open_gate()
                        time.sleep(RETRY)
                    else:
                        raise AssertionError(f"unexpected claim reason {got.reason}")
        except BaseException as exc:
            with run.lock:
                run.errors.append(exc)
            stop.set()
            start.abort()
        finally:
            engine.dispose()

    def _call(
        conn: Connection, lease: uuid.UUID, account: uuid.UUID, began: float, asked: float
    ) -> None:
        with run.lock:
            run.claims += 1
            run.grants[account] += 1
            run.waits.append(time.monotonic() - asked)
            if run.pause_done is not None and began > run.pause_done:
                run.late_grants += 1
        run.in_flight.enter()
        try:
            status, body = fake.call()
            interpret("measure:getmeas", status, body, EmptyBody)
        except RateLimited as exc:
            # What ``limited`` does with a 601: hold the whole app, committed at once.
            with conn.begin():
                pause(conn, app_id, exc.wait_seconds)
            with run.lock:
                run.pause_done = time.monotonic()
            fake.open_gate()  # the calls held behind the 601 may answer now
        except Transient:
            # A 429: the retry is the worker's next claim, which spends a token of its own.
            with run.lock:
                run.retries_429 += 1
        finally:
            time.sleep(HOLD)
            run.in_flight.leave()  # before the release commits: the count never overstates
            with conn.begin():
                release(conn, lease)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=deadline + 10)
    if any(thread.is_alive() for thread in threads):
        raise AssertionError("a load worker did not finish")
    if run.errors:
        raise run.errors[0]
    return run


def _assert_budget(
    url: str, app_id: int, budget: int, started: datetime, admitted: int, claims: int
) -> None:
    """The exact admission bounds of one drained bucket."""
    remaining, now = _bucket(url, app_id)
    elapsed = now - started.timestamp()
    refill = elapsed * budget / 60
    # Over-admission: never more grants than the budget plus the refill the run had time for.
    assert admitted <= budget + math.floor(refill), (admitted, budget, refill)
    # No lost update: every grant took a token off the stored balance. Grants plus what is
    # left cannot exceed what the bucket was given (rounding is at most half a unit per claim).
    assert admitted + remaining <= budget + refill + ROUNDING * claims + 1e-9
    # Under-admission: the bucket was drained (a claim saw it empty), so it gave its budget.
    assert admitted >= budget
    assert remaining < 1


def _accounts(profile: Profile) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(profile.users)]


@pytest.mark.parametrize("given", profiles())
def test_the_app_budget_holds_under_n_workers_and_m_users(
    given: Profile | None,
    schema: None,
    database_url: str,
    app_database_url: str,
    request: pytest.FixtureRequest,
) -> None:
    profile = resolve(given)
    accounts = _accounts(profile)
    app_id, started = _seed(database_url, profile.budget, accounts)
    # Every 7th call answers HTTP 429: its retry must spend a token like any other call.
    # The fake holds every call (and so its slot) until a claim is refused for want of a slot:
    # the cap is contended on every run, and with a fifth slot a fifth call would be in flight
    # before the gate opens (#479).
    fake = FakeWithings(
        {n: 429 for n in range(7, 10 * profile.budget, 7)}, hold_after=0, timeout=10.0
    )
    run = _drive(
        app_database_url,
        app_id,
        accounts,
        profile.workers,
        fake,
        profile.deadline,
        open_gate_on_busy=True,
    )

    admitted = sum(run.grants.values())
    assert run.drained
    _assert_budget(database_url, app_id, profile.budget, started, admitted, run.claims)
    # Every call the fake Withings saw was a granted claim: no call bypassed the limiter.
    assert fake.calls == admitted
    assert run.retries_429 == len([n for n in range(7, admitted + 1, 7)])
    # The global in-flight cap, contended: claims were refused for want of a slot, and the
    # calls in flight reached the cap but never passed it.
    assert run.busy > 0
    assert run.in_flight.peak == SLOT_CAP
    # No starvation: every waiting account was served, and the round-robin keeps the shares
    # within a couple of grants of each other.
    shares = [run.grants[account] for account in accounts]
    assert min(shares) >= 1, shares
    assert max(shares) - min(shares) <= 2, shares
    record(
        request.config,
        Report(
            f"withings budget {profile.budget}, {profile.users} users",
            profile.name,
            profile.workers,
            admitted=admitted,
            refused=run.refused,
            waits=run.waits,
        ),
    )


@pytest.mark.parametrize("given", profiles())
def test_a_601_holds_every_worker_until_the_pause_ends(
    given: Profile | None,
    schema: None,
    database_url: str,
    app_database_url: str,
    request: pytest.FixtureRequest,
) -> None:
    profile = resolve(given)
    accounts = _accounts(profile)
    app_id, started = _seed(database_url, profile.budget, accounts)
    trip = max(1, profile.budget // 2)
    fake = FakeWithings({trip: 601}, hold_after=trip, timeout=profile.deadline)

    held = _drive(
        app_database_url,
        app_id,
        accounts,
        profile.workers,
        fake,
        profile.deadline,
        stop_after_paused=3,
    )
    assert held.pause_done is not None
    assert not held.drained
    # No claim that started after the pause committed was granted, by any worker.
    assert held.late_grants == 0
    # Before the pause: the tripping call and at most the three calls that took the other
    # slots (the fake holds them until the pause has committed, so no fifth call gets in).
    assert trip <= sum(held.grants.values()) <= trip + SLOT_CAP - 1
    assert fake.calls == sum(held.grants.values())
    # Every worker was told to come back at the same resume time, the 601's 30 seconds on.
    assert held.paused_claims >= 3 * profile.workers
    assert len(held.paused_retry_at) == 1
    resume = next(iter(held.paused_retry_at))
    assert resume - datetime.now(UTC) > timedelta(seconds=20)

    # The fake clock: the superuser ends the hold (the app role cannot), then the rest drains.
    with engine_for(database_url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE withings_rate_buckets SET paused_until = clock_timestamp() "
                "- interval '1 second' WHERE app_id = :app"
            ),
            {"app": app_id},
        )
    resumed = _drive(app_database_url, app_id, accounts, profile.workers, fake, profile.deadline)
    assert resumed.drained
    assert resumed.paused_claims == 0
    admitted = sum(held.grants.values()) + sum(resumed.grants.values())
    _assert_budget(
        database_url, app_id, profile.budget, started, admitted, held.claims + resumed.claims
    )
    assert fake.calls == admitted
    record(
        request.config,
        Report(
            f"withings 601 hold, budget {profile.budget}",
            profile.name,
            profile.workers,
            admitted=admitted,
            refused=held.refused + resumed.refused,
            waits=held.waits + resumed.waits,
        ),
    )


def test_the_load_variants_are_wired(request: pytest.FixtureRequest) -> None:
    """The CI variant runs in the default suite with at least 50 callers; the full one is
    marked ``load`` and the default run deselects it."""
    params = profiles()
    assert [p.id for p in params] == ["ci", "full"]  # type: ignore[attr-defined]
    ci = params[0].values[0]  # type: ignore[attr-defined]
    assert isinstance(ci, Profile)
    assert ci.workers >= 50
    assert not params[0].marks  # type: ignore[attr-defined]
    assert [m.name for m in params[1].marks] == ["load"]  # type: ignore[attr-defined]
    assert "not load" in " ".join(request.config.getini("addopts"))


def test_the_network_guard_covers_the_load_tests() -> None:
    """The suite's guard is on here too: the load never reaches Withings, only the fake."""
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("wbsapi.withings.net", 443)
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("192.0.2.1", 443), timeout=0.1)


def test_no_load_test_lifts_the_network_guard(request: pytest.FixtureRequest) -> None:
    here = Path(__file__).resolve().parent
    load_items = [i for i in request.session.items if here in Path(str(i.path)).resolve().parents]
    assert load_items
    assert not [i.nodeid for i in load_items if i.get_closest_marker("allow_network")]
