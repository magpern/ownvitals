"""The per-user monthly AI cost cap against real PostgreSQL (ADR 0014 section 7, #205).

Acceptance criteria of #205:

1. 50 concurrent reservations against a cap that fits 10 admit exactly 10, and ``spent +
   reserved`` never exceeds either cap, sampled during the run
   (``test_fifty_concurrent_reservations_admit_exactly_ten``, and the global-cap variant).
2. A crash between reserve and settle never lets spend exceed the cap; the reaper releases only
   expired reservations (``test_the_reaper_*``, ``test_settle_then_crash_*``).
3. RLS: one user's ledger is invisible to another (``test_one_user_s_ledger_is_invisible_*``);
   each definer function has a test per caller role (``test_ai_functions.py``).

Plus the ADR's list: double settle, release then settle, a January reservation settled in
February, the global cap binding first, the CHECKs, an overrun charged in full and blocking the
month, the app role's privileges, caps of 0, no context. Synthetic users and jobs only.
"""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from aitools import (  # noqa: F401 - fixtures
    RELEASE,
    SETTLE,
    SETTLE_LOST,
    ai_fixture,
    app_engine,
    call,
    counters,
    ledger_matches_counters,
    reservation,
    reserve,
    running_job,
)
from jobtools import (  # noqa: F401 - fixtures
    admin_fixture,
    admin_url,
    as_user,
    engine_for,
    ensure_user,
    new_user,
    queue_schema_fixture,
)

REFUSED = "42501"
INVALID = "22023"
CHECK = "23514"
BIG = 10**12


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def _month(su: Connection) -> object:
    return su.execute(text("SELECT date_trunc('month', now() AT TIME ZONE 'UTC')::date")).scalar()


# --- reserve ----------------------------------------------------------------------------------


def test_a_reservation_counts_against_both_rows(ai: Connection, app_database_url: str) -> None:
    user = new_user()
    ensure_user(ai, user)
    job = running_job(ai, user)
    with app_engine(app_database_url) as app:
        rid = reserve(app, user, job, 700, user_cap=1000, global_cap=5000)
    assert rid is not None
    month = _month(ai)
    assert counters(ai, "ai_spend", owner_id=user, month=month) == (700, 0)
    assert counters(ai, "ai_spend_global", month=month) == (700, 0)
    row = reservation(ai, rid)
    assert (row.owner_id, row.month, row.amount, row.state, row.actual) == (
        user,
        month,
        700,
        "reserved",
        None,
    )
    assert (row.job_id, row.lease_version) == (job, 1)


def test_a_reservation_over_the_user_cap_is_refused(ai: Connection, app_database_url: str) -> None:
    user = new_user()
    job = running_job(ai, user)
    with app_engine(app_database_url) as app:
        assert reserve(app, user, job, 600, user_cap=1000, global_cap=BIG) is not None
        assert reserve(app, user, job, 401, user_cap=1000, global_cap=BIG) is None
        assert reserve(app, user, job, 400, user_cap=1000, global_cap=BIG) is not None
        assert reserve(app, user, job, 1, user_cap=1000, global_cap=BIG) is None
    assert counters(ai, "ai_spend", owner_id=user, month=_month(ai)) == (1000, 0)


def test_the_global_cap_binds_before_the_user_cap(ai: Connection, app_database_url: str) -> None:
    first, second = new_user(), new_user()
    jobs = {first: running_job(ai, first), second: running_job(ai, second)}
    with app_engine(app_database_url) as app:
        assert reserve(app, first, jobs[first], 600, user_cap=1000, global_cap=1000) is not None
        # The second user is far below their own cap; the month's total is not.
        assert reserve(app, second, jobs[second], 500, user_cap=1000, global_cap=1000) is None
        assert reserve(app, second, jobs[second], 400, user_cap=1000, global_cap=1000) is not None
    assert counters(ai, "ai_spend_global", month=_month(ai)) == (1000, 0)


@pytest.mark.parametrize(("user_cap", "global_cap"), [(0, 0), (0, BIG), (BIG, 0)])
def test_a_cap_of_zero_refuses_everything(
    ai: Connection, app_database_url: str, user_cap: int, global_cap: int
) -> None:
    user = new_user()
    job = running_job(ai, user)
    with app_engine(app_database_url) as app:
        assert reserve(app, user, job, 1, user_cap=user_cap, global_cap=global_cap) is None
    assert ai.execute(text("SELECT count(*) FROM ai_reservations")).scalar() == 0
    assert counters(ai, "ai_spend", owner_id=user, month=_month(ai)) == (0, 0)


def test_reserve_without_a_user_context_is_refused(ai: Connection, app_database_url: str) -> None:
    user = new_user()
    job = running_job(ai, user)
    with app_engine(app_database_url) as app, pytest.raises(DBAPIError) as raised:
        call(app, None, "SELECT ai_reserve(1, :job, 10, 10)", job=job)
    assert _sqlstate(raised.value) == REFUSED
    assert ai.execute(text("SELECT count(*) FROM ai_spend")).scalar() == 0


def test_reserve_needs_the_context_user_s_running_job(
    ai: Connection, app_database_url: str
) -> None:
    """Another user's job, a queued job, an expired lease or an unknown id: refused, nothing
    written."""
    user, other = new_user(), new_user()
    ensure_user(ai, user)
    others = running_job(ai, other)
    queued = running_job(ai, user)
    ai.execute(
        text(
            "UPDATE jobs SET state = 'queued', leased_by = NULL, lease_expires_at = NULL, "
            "heartbeat_at = NULL WHERE id = :j"
        ),
        {"j": queued},
    )
    expired = running_job(ai, user)
    ai.execute(
        text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"),
        {"j": expired},
    )
    with app_engine(app_database_url) as app:
        for job in (others, queued, expired, uuid.uuid4()):
            with pytest.raises(DBAPIError) as raised:
                reserve(app, user, job, 1, user_cap=BIG, global_cap=BIG)
            assert _sqlstate(raised.value) == INVALID
    assert ai.execute(text("SELECT count(*) FROM ai_reservations")).scalar() == 0


@pytest.mark.parametrize(
    ("amount", "user_cap", "global_cap"),
    [(0, 10, 10), (-1, 10, 10), (None, 10, 10), (1, -1, 10), (1, 10, -1), (1, None, 10)],
)
def test_reserve_refuses_inputs_outside_its_bounds(
    ai: Connection,
    app_database_url: str,
    amount: int | None,
    user_cap: int | None,
    global_cap: int | None,
) -> None:
    user = new_user()
    job = running_job(ai, user)
    with app_engine(app_database_url) as app, pytest.raises(DBAPIError) as raised:
        call(
            app,
            user,
            "SELECT ai_reserve(CAST(:a AS bigint), :job, CAST(:u AS bigint), CAST(:g AS bigint))",
            a=amount,
            job=job,
            u=user_cap,
            g=global_cap,
        )
    assert _sqlstate(raised.value) == INVALID


# --- settle and release -----------------------------------------------------------------------


def test_settle_moves_reserved_to_spent_once(ai: Connection, app_database_url: str) -> None:
    """Double settle: the second changes nothing, whatever it asks."""
    user = new_user()
    job = running_job(ai, user)
    month = _month(ai)
    with app_engine(app_database_url) as app:
        rid = reserve(app, user, job, 1000, user_cap=BIG, global_cap=BIG)
        assert call(app, user, SETTLE, id=rid, actual=300) is True
        assert call(app, user, SETTLE, id=rid, actual=900) is False
        assert call(app, user, RELEASE, id=rid) is False
    assert counters(ai, "ai_spend", owner_id=user, month=month) == (0, 300)
    assert counters(ai, "ai_spend_global", month=month) == (0, 300)
    row = reservation(ai, rid)
    assert (row.state, row.actual) == ("settled", 300)
    assert row.ended_at is not None


def test_release_then_settle_changes_nothing(ai: Connection, app_database_url: str) -> None:
    user = new_user()
    job = running_job(ai, user)
    month = _month(ai)
    with app_engine(app_database_url) as app:
        rid = reserve(app, user, job, 1000, user_cap=BIG, global_cap=BIG)
        assert call(app, user, RELEASE, id=rid) is True
        assert call(app, user, SETTLE, id=rid, actual=1000) is False
        assert call(app, user, RELEASE, id=rid) is False
    assert counters(ai, "ai_spend", owner_id=user, month=month) == (0, 0)
    assert counters(ai, "ai_spend_global", month=month) == (0, 0)
    assert (reservation(ai, rid).state, reservation(ai, rid).actual) == ("released", None)


def test_another_user_cannot_settle_or_release_a_reservation(
    ai: Connection, app_database_url: str
) -> None:
    user, other = new_user(), new_user()
    ensure_user(ai, other)
    job = running_job(ai, user)
    with app_engine(app_database_url) as app:
        rid = reserve(app, user, job, 1000, user_cap=BIG, global_cap=BIG)
        assert call(app, other, SETTLE, id=rid, actual=0) is False
        assert call(app, other, RELEASE, id=rid) is False
        for sql, params in ((SETTLE, {"id": rid, "actual": 0}), (RELEASE, {"id": rid})):
            with pytest.raises(DBAPIError) as raised:
                call(app, None, sql, **params)
            assert _sqlstate(raised.value) == REFUSED
    assert reservation(ai, rid).state == "reserved"
    assert counters(ai, "ai_spend", owner_id=user, month=_month(ai)) == (1000, 0)


def test_settle_refuses_a_negative_actual(ai: Connection, app_database_url: str) -> None:
    user = new_user()
    job = running_job(ai, user)
    with app_engine(app_database_url) as app:
        rid = reserve(app, user, job, 1000, user_cap=BIG, global_cap=BIG)
        with pytest.raises(DBAPIError) as raised:
            call(app, user, SETTLE, id=rid, actual=-1)
    assert _sqlstate(raised.value) == INVALID
    assert reservation(ai, rid).state == "reserved"


def test_an_overrun_is_charged_in_full_and_ends_the_month(
    ai: Connection, app_database_url: str
) -> None:
    """The bound was wrong: the actual is charged even past the cap, and no further request is
    reserved that month."""
    user = new_user()
    job = running_job(ai, user)
    month = _month(ai)
    with app_engine(app_database_url) as app:
        rid = reserve(app, user, job, 800, user_cap=1000, global_cap=1000)
        assert call(app, user, SETTLE, id=rid, actual=1300) is True
        assert reserve(app, user, job, 1, user_cap=1000, global_cap=1000) is None
        assert reserve(app, user, job, 1, user_cap=BIG, global_cap=1000) is None
    assert counters(ai, "ai_spend", owner_id=user, month=month) == (0, 1300)
    assert counters(ai, "ai_spend_global", month=month) == (0, 1300)


def test_a_january_reservation_settled_in_february_charges_january(
    ai: Connection, app_database_url: str
) -> None:
    """The reservation's own month is charged, whatever the month of the settle."""
    user = new_user()
    job = running_job(ai, user)
    current = _month(ai)
    with app_engine(app_database_url) as app:
        rid = reserve(app, user, job, 500, user_cap=BIG, global_cap=BIG)
        # Move the reservation, with the counters it holds, to an earlier month (as if it had
        # been made in January and the settle runs in February).
        for statement in (
            "INSERT INTO ai_spend_global (month, reserved) VALUES ('2026-01-01', 500)",
            "UPDATE ai_spend_global SET reserved = 0 WHERE month = :m",
            "UPDATE ai_spend SET month = '2026-01-01' WHERE owner_id = :o",
            "UPDATE ai_reservations SET month = '2026-01-01' WHERE id = :r",
        ):
            ai.execute(text(statement), {"m": current, "o": user, "r": rid})
        # A reservation in the current month, so both months have a row for this user.
        assert reserve(app, user, job, 100, user_cap=BIG, global_cap=BIG) is not None
        assert call(app, user, SETTLE, id=rid, actual=450) is True
    assert counters(ai, "ai_spend", owner_id=user, month="2026-01-01") == (0, 450)
    assert counters(ai, "ai_spend_global", month="2026-01-01") == (0, 450)
    assert counters(ai, "ai_spend", owner_id=user, month=current) == (100, 0)
    assert counters(ai, "ai_spend_global", month=current) == (100, 0)


# --- the reaper and crashes -------------------------------------------------------------------


def test_the_reaper_settles_only_reservations_whose_attempt_is_over(
    ai: Connection, app_database_url: str
) -> None:
    """Running under its lease: kept. Lease expired, a newer lease, the job finished or gone:
    settled at the full amount."""
    user = new_user()
    jobs = {name: running_job(ai, user) for name in ("live", "expired", "newer", "done", "gone")}
    with app_engine(app_database_url) as app:
        ids = {
            name: reserve(app, user, job, 100, user_cap=BIG, global_cap=BIG)
            for name, job in jobs.items()
        }
        ai.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"),
            {"j": jobs["expired"]},
        )
        ai.execute(
            text("UPDATE jobs SET lease_version = lease_version + 1 WHERE id = :j"),
            {"j": jobs["newer"]},
        )
        ai.execute(
            text(
                "UPDATE jobs SET state = 'completed', leased_by = NULL, lease_expires_at = NULL, "
                "heartbeat_at = NULL, finished_at = now() WHERE id = :j"
            ),
            {"j": jobs["done"]},
        )
        ai.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jobs["gone"]})
        assert call(app, None, SETTLE_LOST, n=100) == 4
        assert call(app, None, SETTLE_LOST, n=100) == 0
    states = {
        name: (reservation(ai, rid).state, reservation(ai, rid).actual) for name, rid in ids.items()
    }
    assert states == {
        "live": ("reserved", None),
        "expired": ("settled", 100),
        "newer": ("settled", 100),
        "done": ("settled", 100),
        "gone": ("settled", 100),
    }
    assert counters(ai, "ai_spend", owner_id=user, month=_month(ai)) == (100, 400)
    ledger_matches_counters(ai)


def test_the_reaper_takes_at_most_its_limit(ai: Connection, app_database_url: str) -> None:
    user = new_user()
    job = running_job(ai, user)
    with app_engine(app_database_url) as app:
        for _ in range(3):
            reserve(app, user, job, 10, user_cap=BIG, global_cap=BIG)
        ai.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": job})
        assert call(app, None, SETTLE_LOST, n=2) == 2
        assert call(app, None, SETTLE_LOST, n=2) == 1
        for limit in (0, 1001):
            with pytest.raises(DBAPIError) as raised:
                call(app, None, SETTLE_LOST, n=limit)
            assert _sqlstate(raised.value) == INVALID


def test_a_crash_after_reserve_is_charged_the_reservation_and_holds_the_cap(
    ai: Connection, app_database_url: str
) -> None:
    """The worker dies after reserving: the reservation keeps counting until the reaper charges
    it in full, so spending never passes the cap."""
    user = new_user()
    job = running_job(ai, user)
    month = _month(ai)
    with app_engine(app_database_url) as app:
        assert reserve(app, user, job, 600, user_cap=1000, global_cap=1000) is not None
        # The worker is gone. While the lease runs, the reservation still holds the budget.
        assert reserve(app, user, job, 500, user_cap=1000, global_cap=1000) is None
        ai.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"),
            {"j": job},
        )
        assert call(app, None, SETTLE_LOST, n=10) == 1
        assert counters(ai, "ai_spend", owner_id=user, month=month) == (0, 600)
        # The next attempt still sees 600 spent.
        retry = running_job(ai, user, lease_version=2)
        assert reserve(app, user, retry, 500, user_cap=1000, global_cap=1000) is None
        assert reserve(app, user, retry, 400, user_cap=1000, global_cap=1000) is not None


def test_settle_then_crash_leaves_nothing_for_the_reaper(
    ai: Connection, app_database_url: str
) -> None:
    """The settle committed; the worker dies before the job is marked done. The reaper later
    finds the attempt over and changes nothing."""
    user = new_user()
    job = running_job(ai, user)
    month = _month(ai)
    with app_engine(app_database_url) as app:
        rid = reserve(app, user, job, 1000, user_cap=BIG, global_cap=BIG)
        assert call(app, user, SETTLE, id=rid, actual=250) is True
        ai.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"),
            {"j": job},
        )
        assert call(app, None, SETTLE_LOST, n=10) == 0
        # A late settle by a worker that lost its lease after the reaper: no change either.
        assert call(app, user, SETTLE, id=rid, actual=999) is False
    assert counters(ai, "ai_spend", owner_id=user, month=month) == (0, 250)
    assert (reservation(ai, rid).state, reservation(ai, rid).actual) == ("settled", 250)


# --- concurrency ------------------------------------------------------------------------------


def _sampler(stop: threading.Event, caps: dict[str, int], seen: list[str]) -> None:
    """Reads both counters while reservations run; records any moment over a cap."""
    with engine_for(admin_url()) as engine, engine.connect() as su:
        while not stop.is_set():
            for owner, reserved, spent in su.execute(
                text("SELECT owner_id::text, reserved, spent FROM ai_spend")
            ).all():
                if reserved + spent > caps["user"]:
                    seen.append(f"user {owner}: {reserved + spent}")
            for reserved, spent in su.execute(
                text("SELECT reserved, spent FROM ai_spend_global")
            ).all():
                if reserved + spent > caps["global"]:
                    seen.append(f"global: {reserved + spent}")
            su.commit()
            time.sleep(0.002)


def _race(
    app_database_url: str, calls: list[tuple[uuid.UUID, uuid.UUID]], caps: dict[str, int]
) -> list[uuid.UUID | None]:
    """Run one reservation of 100 per entry, all released at once by a barrier."""
    barrier = threading.Barrier(len(calls))
    stop = threading.Event()
    over: list[str] = []
    sampler = threading.Thread(target=_sampler, args=(stop, caps, over))
    with app_engine(app_database_url) as app:

        def one(entry: tuple[uuid.UUID, uuid.UUID]) -> uuid.UUID | None:
            user, job = entry
            with app.begin() as conn:
                as_user(conn, user)
                barrier.wait(timeout=30)
                return conn.execute(
                    text("SELECT ai_reserve(100, :job, :u, :g)"),
                    {"job": job, "u": caps["user"], "g": caps["global"]},
                ).scalar_one()

        sampler.start()
        try:
            with ThreadPoolExecutor(max_workers=len(calls)) as pool:
                results = list(pool.map(one, calls))
        finally:
            stop.set()
            sampler.join()
    assert over == []
    return results


def test_fifty_concurrent_reservations_admit_exactly_ten(
    ai: Connection, app_database_url: str
) -> None:
    user = new_user()
    job = running_job(ai, user)
    results = _race(app_database_url, [(user, job)] * 50, {"user": 1000, "global": BIG})
    assert sum(r is not None for r in results) == 10
    assert counters(ai, "ai_spend", owner_id=user, month=_month(ai)) == (1000, 0)
    assert ai.execute(text("SELECT count(*) FROM ai_reservations")).scalar() == 10
    ledger_matches_counters(ai)


def test_concurrent_reservations_of_many_users_hold_the_global_cap(
    ai: Connection, app_database_url: str
) -> None:
    users = [new_user() for _ in range(5)]
    jobs = {user: running_job(ai, user) for user in users}
    calls = [(user, jobs[user]) for user in users for _ in range(10)]
    results = _race(app_database_url, calls, {"user": 500, "global": 1000})
    assert sum(r is not None for r in results) == 10
    assert counters(ai, "ai_spend_global", month=_month(ai)) == (1000, 0)
    ledger_matches_counters(ai)


def test_reserve_settle_release_and_the_reaper_run_together_without_deadlock(
    ai: Connection, app_database_url: str
) -> None:
    """Every function on the same user and month at once: no deadlock, no lost update."""
    user, other = new_user(), new_user()
    job, gone, others = running_job(ai, user), running_job(ai, user), running_job(ai, other)
    with app_engine(app_database_url) as app:
        open_ids = [reserve(app, user, job, 50, user_cap=BIG, global_cap=BIG) for _ in range(20)]
        lost = [reserve(app, user, gone, 50, user_cap=BIG, global_cap=BIG) for _ in range(10)]
        ai.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": gone})
        barrier = threading.Barrier(40)

        def work(i: int) -> object:
            with app.begin() as conn:
                if i < 10:
                    as_user(conn, user)
                    barrier.wait(timeout=30)
                    return conn.execute(
                        text("SELECT ai_reserve(50, :j, :c, :c)"), {"j": job, "c": BIG}
                    ).scalar_one()
                if i < 20:
                    as_user(conn, user)
                    barrier.wait(timeout=30)
                    return conn.execute(
                        text(SETTLE), {"id": open_ids[i - 10], "actual": 40}
                    ).scalar_one()
                if i < 30:
                    as_user(conn, user)
                    barrier.wait(timeout=30)
                    return conn.execute(text(RELEASE), {"id": open_ids[i - 10]}).scalar_one()
                if i < 35:
                    barrier.wait(timeout=30)
                    return conn.execute(text(SETTLE_LOST), {"n": 3}).scalar_one()
                as_user(conn, other)
                barrier.wait(timeout=30)
                return conn.execute(
                    text("SELECT ai_reserve(50, :j, :c, :c)"), {"j": others, "c": BIG}
                ).scalar_one()

        with ThreadPoolExecutor(max_workers=40) as pool:
            results = list(pool.map(work, range(40)))
    assert all(r is not None for r in results[:10])
    assert results[10:30] == [True] * 20
    # Concurrent reapers find the same candidates and serialize on the month's row: each lost
    # reservation is settled by exactly one of them, the rest by the next run.
    reaped = sum(int(r) for r in results[30:35])  # type: ignore[call-overload]
    assert 3 <= reaped <= len(lost)
    with app_engine(app_database_url) as app:
        assert call(app, None, SETTLE_LOST, n=100) == len(lost) - reaped
    assert all(reservation(ai, rid).state == "settled" for rid in lost)
    assert all(reservation(ai, rid).actual == 50 for rid in lost)
    ledger_matches_counters(ai)


# --- the tables: CHECKs, privileges, RLS ------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ai_spend SET reserved = -1",
        "UPDATE ai_spend SET spent = -1",
        "UPDATE ai_spend_global SET reserved = -1",
        "UPDATE ai_spend_global SET spent = -1",
        "UPDATE ai_reservations SET amount = -1",
        "UPDATE ai_reservations SET state = 'settled', actual = -1, ended_at = now()",
        "UPDATE ai_reservations SET state = 'settled', ended_at = now()",
        "UPDATE ai_reservations SET state = 'reserved_twice'",
        "UPDATE ai_spend SET month = '2026-01-02'",
    ],
)
def test_the_checks_refuse_a_negative_or_inconsistent_row(
    ai: Connection, app_database_url: str, statement: str
) -> None:
    user = new_user()
    job = running_job(ai, user)
    with app_engine(app_database_url) as app:
        reserve(app, user, job, 10, user_cap=BIG, global_cap=BIG)
    with pytest.raises(DBAPIError) as raised:
        ai.execute(text(statement))
    assert _sqlstate(raised.value) == CHECK


def test_the_app_role_cannot_write_the_ledger_or_read_the_global_row(
    ai: Connection, app_database_url: str
) -> None:
    user = new_user()
    job = running_job(ai, user)
    with app_engine(app_database_url) as app:
        rid = reserve(app, user, job, 10, user_cap=BIG, global_cap=BIG)
        statements = [
            "INSERT INTO ai_spend (owner_id, month) VALUES (:u, '2026-02-01')",
            "UPDATE ai_spend SET reserved = 0",
            "DELETE FROM ai_spend",
            "INSERT INTO ai_reservations (owner_id, month, amount, job_id, lease_version) "
            "VALUES (:u, '2026-02-01', 1, gen_random_uuid(), 0)",
            "UPDATE ai_reservations SET state = 'released', ended_at = now()",
            "DELETE FROM ai_reservations",
            "SELECT * FROM ai_spend_global",
            "UPDATE ai_spend_global SET spent = 0",
            "TRUNCATE ai_spend",
        ]
        for statement in statements:
            with app.begin() as conn:
                as_user(conn, user)
                with pytest.raises(DBAPIError) as raised:
                    conn.execute(text(statement), {"u": user})
            assert _sqlstate(raised.value) == REFUSED, statement
    assert reservation(ai, rid).state == "reserved"


def test_one_user_s_ledger_is_invisible_to_another(ai: Connection, app_database_url: str) -> None:
    """Forced RLS: the app role sees only the context user's rows, and none without one."""
    user, other = new_user(), new_user()
    ensure_user(ai, other)
    job = running_job(ai, user)
    with app_engine(app_database_url) as app:
        rid = reserve(app, user, job, 10, user_cap=BIG, global_cap=BIG)
        for viewer, want in ((user, 1), (other, 0), (None, 0)):
            with app.begin() as conn:
                if viewer is not None:
                    as_user(conn, viewer)
                spend = conn.execute(text("SELECT count(*) FROM ai_spend")).scalar()
                held = conn.execute(
                    text("SELECT count(*) FROM ai_reservations WHERE id = :r"), {"r": rid}
                ).scalar()
            assert (spend, held) == (want, want)


def test_a_deleted_account_takes_its_ledger_and_leaves_the_global_spend(
    ai: Connection, app_database_url: str
) -> None:
    user = new_user()
    job = running_job(ai, user)
    with app_engine(app_database_url) as app:
        rid = reserve(app, user, job, 10, user_cap=BIG, global_cap=BIG)
        call(app, user, SETTLE, id=rid, actual=7)
    ai.execute(text("DELETE FROM users WHERE id = :u"), {"u": user})
    assert ai.execute(text("SELECT count(*) FROM ai_spend")).scalar() == 0
    assert ai.execute(text("SELECT count(*) FROM ai_reservations")).scalar() == 0
    assert counters(ai, "ai_spend_global", month=_month(ai)) == (0, 7)


def test_reserve_buckets_by_the_utc_month(ai: Connection, app_database_url: str) -> None:
    """The month is the UTC one, whatever the session's time zone."""
    user = new_user()
    job = running_job(ai, user)
    with app_engine(app_database_url) as app, app.begin() as conn:
        conn.execute(text("SET LOCAL TimeZone = 'Pacific/Kiritimati'"))
        as_user(conn, user)
        rid = conn.execute(text("SELECT ai_reserve(1, :j, 10, 10)"), {"j": job}).scalar_one()
    now = datetime.now(UTC)
    assert (
        reservation(ai, rid).month == now.date().replace(day=1)
        or (now + timedelta(seconds=5)).day == 1
    )
