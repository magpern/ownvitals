"""The rate claim reads the clock under the bucket lock (#470). Synthetic app and accounts only.

A claim that waited for the bucket row used the time it was called at, older than the
``refilled_at`` the lock holder wrote. It moved ``refilled_at`` back, and the next claim earned
that interval's refill a second time: under contention the budget leaked (found by the load
harness of #462). Deterministic: the test holds the lock itself, waits until the claim is
blocked on it, and lets it go only after a known gap.
"""

import threading
import time
import uuid
from datetime import datetime

from sqlalchemy import text

from jobtools import engine_for
from ownvitals.withings.rate import LANE_SYNC_NOW, Claim, claim
from withingsschema import new_app

BUDGET = 60  # one token a second: the gap below is worth GAP tokens of refill
GAP = 1.5  # seconds the blocked claim waits behind the lock
BLOCKED_TIMEOUT = 10.0


def _bucket(url: str, app_id: int) -> tuple[float, datetime]:
    with engine_for(url) as engine, engine.connect() as conn:
        remaining, refilled = conn.execute(
            text("SELECT remaining, refilled_at FROM withings_rate_buckets WHERE app_id = :app"),
            {"app": app_id},
        ).one()
    return float(remaining), refilled


def _wait_until_blocked(url: str) -> None:
    """Until a claim waits on a lock (the bucket row this test holds)."""
    ends = time.monotonic() + BLOCKED_TIMEOUT
    with engine_for(url) as engine:
        while time.monotonic() < ends:
            with engine.connect() as conn:
                waiting = conn.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                        "AND query LIKE '%withings_rate_claim%' AND pid <> pg_backend_pid()"
                    )
                ).scalar_one()
            if waiting:
                return
            time.sleep(0.02)
    raise AssertionError("the claim never waited for the bucket lock")


def test_a_claim_held_behind_the_lock_cannot_earn_the_refill_again(
    schema: None, database_url: str, app_database_url: str
) -> None:
    app_id = new_app(database_url)
    with engine_for(database_url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO withings_rate_buckets (app_id, remaining, budget, refilled_at) "
                "VALUES (:app, 0, :budget, clock_timestamp())"
            ),
            {"app": app_id, "budget": BUDGET},
        )
    account = uuid.uuid4()  # one account: a second one would only be told to wait its turn
    blocked: list[Claim] = []

    def blocked_claim() -> None:
        with engine_for(app_database_url) as engine, engine.begin() as conn:
            blocked.append(claim(conn, app_id, account, LANE_SYNC_NOW))

    with engine_for(database_url) as engine, engine.connect() as holder:
        lock = holder.begin()
        holder.execute(
            text("SELECT 1 FROM withings_rate_buckets WHERE app_id = :app FOR UPDATE"),
            {"app": app_id},
        )
        waiter = threading.Thread(target=blocked_claim, daemon=True)
        waiter.start()
        _wait_until_blocked(database_url)
        time.sleep(GAP)
        # The holder spends the bucket and refills it to now, as a claim holding it would.
        held_until = holder.execute(
            text(
                "UPDATE withings_rate_buckets SET remaining = 0, refilled_at = clock_timestamp() "
                "WHERE app_id = :app RETURNING refilled_at"
            ),
            {"app": app_id},
        ).scalar_one()
        lock.commit()
    waiter.join(timeout=BLOCKED_TIMEOUT)
    assert not waiter.is_alive()

    # The claim that waited saw an empty bucket: the gap was spent by the holder's refill.
    assert [c.reason for c in blocked] == ["empty"]
    remaining, refilled = _bucket(database_url, app_id)
    assert refilled >= held_until  # refilled_at did not move back to when the claim was called
    assert remaining < 0.5

    # The next claim, at once: only the moments since the holder's refill count, not the gap.
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        after = claim(conn, app_id, account, LANE_SYNC_NOW)
    assert after.reason == "empty"
    assert not after.granted
    remaining, _ = _bucket(database_url, app_id)
    assert remaining < GAP * BUDGET / 60 / 2


def test_refilled_at_never_moves_backwards(
    schema: None, database_url: str, app_database_url: str
) -> None:
    """A ``refilled_at`` ahead of the clock (a clock step back) is kept, and earns nothing."""
    app_id = new_app(database_url)
    with engine_for(database_url) as engine, engine.begin() as conn:
        ahead = conn.execute(
            text(
                "INSERT INTO withings_rate_buckets (app_id, remaining, budget, refilled_at) "
                "VALUES (:app, 0, :budget, clock_timestamp() + interval '1 minute') "
                "RETURNING refilled_at"
            ),
            {"app": app_id, "budget": BUDGET},
        ).scalar_one()
    account = uuid.uuid4()
    for _ in range(2):
        with engine_for(app_database_url) as engine, engine.begin() as conn:
            got = claim(conn, app_id, account, LANE_SYNC_NOW)
        assert got.reason == "empty"
        remaining, refilled = _bucket(database_url, app_id)
        assert refilled == ahead
        assert remaining == 0
