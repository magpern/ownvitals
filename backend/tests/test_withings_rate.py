"""The Withings rate limiter (#23). Synthetic apps and account ids only; no health data.

The bucket budget in these tests is lowered by the superuser. The application path cannot
write the tables, and it cannot raise the budget above 100.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from math import isfinite
from random import Random

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from jobtools import engine_for
from ownvitals.jobs import withings as job_lanes
from ownvitals.withings.errors import (
    DEFAULT_RATE_LIMIT_WAIT,
    MAX_RATE_LIMIT_WAIT,
    RateLimited,
    RetryBudget,
    clamp_rate_wait,
    interpret,
)
from ownvitals.withings.models import EmptyBody
from ownvitals.withings.rate import (
    LANE_BACKFILL,
    LANE_POLL,
    LANE_RECONCILE,
    LANE_SYNC_NOW,
    LANE_WEBHOOK,
    claim,
    claim_code_exchange,
    pause,
    release,
)
from withingsschema import new_app

WORKERS = 8


def test_lanes_match_the_job_queue() -> None:
    assert (LANE_SYNC_NOW, LANE_WEBHOOK, LANE_POLL, LANE_RECONCILE, LANE_BACKFILL) == (
        job_lanes.LANE_SYNC_NOW,
        job_lanes.LANE_WEBHOOK,
        job_lanes.LANE_POLL,
        job_lanes.LANE_RECONCILE,
        job_lanes.LANE_BACKFILL,
    )


def test_a_provider_wait_cannot_be_infinite_or_negative() -> None:
    assert clamp_rate_wait(float("inf")) == MAX_RATE_LIMIT_WAIT
    assert clamp_rate_wait(float("1e999")) == MAX_RATE_LIMIT_WAIT
    assert clamp_rate_wait(float("-inf")) == DEFAULT_RATE_LIMIT_WAIT
    assert clamp_rate_wait(-5) == DEFAULT_RATE_LIMIT_WAIT
    assert clamp_rate_wait(float("nan")) == DEFAULT_RATE_LIMIT_WAIT
    assert clamp_rate_wait(17) == 17
    huge = b'{"status": 601, "body": {"wait_seconds": 1e999}}'
    with pytest.raises(RateLimited) as caught:
        interpret("measure:getmeas", 200, huge, EmptyBody)
    assert caught.value.wait_seconds == MAX_RATE_LIMIT_WAIT
    negative = b'{"status": 601, "body": {"wait_seconds": -5}}'
    with pytest.raises(RateLimited) as caught:
        interpret("measure:getmeas", 200, negative, EmptyBody)
    assert caught.value.wait_seconds == DEFAULT_RATE_LIMIT_WAIT


def test_a_long_retry_budget_does_not_overflow() -> None:
    budget = RetryBudget(retries=2, max_delay=60)
    budget.used = 10_000
    delay = budget.next_delay(Random(0), now=0.0)  # noqa: S311 - jitter only, not a secret
    assert delay is not None
    assert isfinite(delay)
    assert 0 < delay <= 60 * 1.2


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
        value = conn.execute(
            text("SELECT remaining FROM withings_rate_buckets WHERE app_id = :app"),
            {"app": app_id},
        ).scalar_one()
    return float(value)


def test_concurrent_workers_cannot_pass_the_budget_in_one_minute(
    schema: None, database_url: str, app_database_url: str
) -> None:
    """N workers, one bucket. A short run has no time to refill a whole extra token."""
    budget = 8
    app_id = _seed(database_url, budget)
    account = uuid.uuid4()
    with engine_for(database_url) as engine, engine.connect() as conn:
        started = conn.execute(text("SELECT clock_timestamp()")).scalar_one()

    def hammer(_: int) -> int:
        taken = 0
        engine = create_engine(app_database_url, pool_size=1, max_overflow=0)
        try:
            with engine.connect() as conn:
                for _step in range(budget + 5):
                    with conn.begin():
                        got = claim(conn, app_id, account, LANE_SYNC_NOW)
                        if got.reason == "granted" and got.lease_id is not None:
                            release(conn, got.lease_id)
                            taken += 1
                        elif got.reason in {"busy", "wait"}:
                            continue
                        else:
                            break
        finally:
            engine.dispose()
        return taken

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        total = sum(pool.map(hammer, range(WORKERS)))
    with engine_for(database_url) as engine, engine.connect() as conn:
        elapsed = conn.execute(
            text("SELECT extract(epoch FROM clock_timestamp() - :started)"),
            {"started": started},
        ).scalar_one()
    allowed = budget + int(float(elapsed) * budget / 60)
    assert total <= allowed
    assert total >= budget
    assert _remaining(database_url, app_id) < 1


def test_a_crashed_call_keeps_its_token_and_frees_its_slot_at_expiry(
    schema: None, database_url: str, app_database_url: str
) -> None:
    app_id = _seed(database_url, budget=6)
    account = uuid.uuid4()
    leases: list[uuid.UUID] = []
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        for _ in range(4):
            got = claim(conn, app_id, account, LANE_SYNC_NOW, lease_seconds=60)
            assert got.granted
            assert got.lease_id is not None
            leases.append(got.lease_id)
    before_busy = _remaining(database_url, app_id)
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        blocked = claim(conn, app_id, account, LANE_SYNC_NOW)
        assert blocked.reason == "busy"
        assert not blocked.granted
    held = _remaining(database_url, app_id)
    # A busy claim spends no token (#278): only the refill since the grants can move it, by
    # far less than one token in this short run.
    assert before_busy <= held < before_busy + 0.5
    with engine_for(database_url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE withings_rate_slots SET lease_until = clock_timestamp() "
                "- interval '1 second' WHERE lease_id = :lease"
            ),
            {"lease": leases[0]},
        )
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        again = claim(conn, app_id, account, LANE_SYNC_NOW)
        assert again.granted
        if again.lease_id is not None:
            release(conn, again.lease_id)
    assert _remaining(database_url, app_id) < held - 0.5


def test_a_601_storm_pauses_and_then_resumes(
    schema: None, database_url: str, app_database_url: str
) -> None:
    app_id = _seed(database_url, budget=4)
    account = uuid.uuid4()
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        first = pause(conn, app_id, 30)
        held = claim(conn, app_id, account, LANE_SYNC_NOW)
        second = pause(conn, app_id, 30)
        still = claim(conn, app_id, account, LANE_POLL)
    assert held.reason == "paused"
    assert still.reason == "paused"
    now = datetime.now(UTC)
    assert first - now < timedelta(seconds=40)
    assert second - now < timedelta(seconds=40)
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        capped = pause(conn, app_id, float("inf"))
        negative = conn.execute(
            text("SELECT withings_rate_pause(:app, -5)"), {"app": app_id}
        ).scalar_one()
        huge = conn.execute(
            text("SELECT withings_rate_pause(:app, 'Infinity'::float8)"), {"app": app_id}
        ).scalar_one()
    assert timedelta(seconds=800) < capped - now < timedelta(seconds=1000)
    assert timedelta(seconds=800) < huge - now < timedelta(seconds=1000)
    # A negative wait does not move the resume into the past; the storm's cap still holds.
    assert negative >= capped - timedelta(seconds=2)
    before = _remaining(database_url, app_id)
    with engine_for(database_url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE withings_rate_buckets SET paused_until = clock_timestamp() "
                "- interval '1 second' WHERE app_id = :app"
            ),
            {"app": app_id},
        )
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        resumed = claim(conn, app_id, account, LANE_SYNC_NOW)
        assert resumed.granted
        assert resumed.lease_id is not None
        release(conn, resumed.lease_id)
    assert _remaining(database_url, app_id) < before


def test_one_accounts_backfill_cannot_starve_anothers_sync(
    schema: None, database_url: str, app_database_url: str
) -> None:
    app_id = _seed(database_url, budget=8)
    backfill_account, sync_account = uuid.uuid4(), uuid.uuid4()
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        priming = claim(conn, app_id, sync_account, LANE_SYNC_NOW)
        assert priming.granted
        assert priming.lease_id is not None
        release(conn, priming.lease_id)
        denied = [claim(conn, app_id, backfill_account, LANE_BACKFILL).reason for _ in range(5)]
        served = claim(conn, app_id, sync_account, LANE_SYNC_NOW)
        assert served.granted
        assert served.lease_id is not None
        release(conn, served.lease_id)
    assert denied == ["wait"] * 5
    # The sync account going idle lets the backfill proceed, and a second account's share holds.
    with engine_for(database_url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE withings_rate_demand SET seen_at = clock_timestamp() "
                "- interval '2 minutes' WHERE account_id = :account"
            ),
            {"account": sync_account},
        )
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        first = claim(conn, app_id, backfill_account, LANE_BACKFILL)
        second = claim(conn, app_id, backfill_account, LANE_BACKFILL)
        other = uuid.uuid4()
        theirs = claim(conn, app_id, other, LANE_BACKFILL)
        ahead = claim(conn, app_id, backfill_account, LANE_BACKFILL)
    assert first.granted
    assert second.granted
    assert theirs.granted
    assert ahead.reason == "wait"


def test_the_same_lane_is_round_robin(
    schema: None, database_url: str, app_database_url: str
) -> None:
    app_id = _seed(database_url, budget=8)
    first, second = uuid.uuid4(), uuid.uuid4()
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        assert claim(conn, app_id, first, LANE_SYNC_NOW).granted
        assert claim(conn, app_id, second, LANE_SYNC_NOW).granted
        # first was granted earlier, so first is due, not second.
        skipped = claim(conn, app_id, second, LANE_SYNC_NOW)
        due = claim(conn, app_id, first, LANE_SYNC_NOW)
    assert skipped.reason == "wait"
    assert due.granted


def test_a_code_exchange_consumes_the_app_budget(
    schema: None, database_url: str, app_database_url: str
) -> None:
    app_id = _seed(database_url, budget=2)
    account = uuid.uuid4()
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        assert claim_code_exchange(conn, app_id, account).granted
        assert claim_code_exchange(conn, app_id, account).granted
        third = claim_code_exchange(conn, app_id, account)
    assert third.reason == "empty"
    assert not third.granted
    assert _remaining(database_url, app_id) < 1


def test_the_app_role_cannot_refund_a_token(
    schema: None, database_url: str, app_database_url: str
) -> None:
    app_id = _seed(database_url, budget=3)
    with engine_for(app_database_url) as engine, pytest.raises(ProgrammingError):  # noqa: SIM117
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE withings_rate_buckets SET remaining = budget WHERE app_id = :app"),
                {"app": app_id},
            )
    message = "unknown app"
    with engine_for(app_database_url) as engine, pytest.raises(DBAPIError, match=message):  # noqa: SIM117
        with engine.begin() as conn:
            claim(conn, app_id + 999_999, uuid.uuid4(), LANE_SYNC_NOW)
