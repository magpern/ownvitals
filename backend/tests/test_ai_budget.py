"""The worker's side of the AI cost cap (``ownvitals.ai.budget``, ADR 0014 section 7, #205):
reserve, settle and release inside the job's fenced transaction, the release rule, the reaper in
the worker's maintenance, the month and the paused message, the cap settings. Synthetic users
and jobs only; the model is never called (there is no model client yet).
"""

import logging
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection

from aitools import ai_fixture, counters, reservation, running_job  # noqa: F401 - fixtures
from ctx import make_context
from jobcore import JobsEnv, jenv_fixture, settings_for  # noqa: F401 - fixtures
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.ai import budget
from ownvitals.ai.budget import Reservation, Settled
from ownvitals.config import Settings
from ownvitals.db import user_session
from ownvitals.jobs.errors import LeaseLost
from ownvitals.jobs.fence import Lease, fenced_session

BIG = 10**12


def _settings(jenv: JobsEnv, user_cap: int, global_cap: int) -> Settings:
    return jenv.settings.model_copy(
        update={
            "ai_user_monthly_cap_microusd": user_cap,
            "ai_global_monthly_cap_microusd": global_cap,
        }
    )


def _lease(ai: Connection) -> Lease:
    owner = uuid.uuid4()
    job = running_job(ai, owner)
    return Lease(job, 1, make_context(owner))


def _month() -> date:
    return budget.month_start(datetime.now(UTC))


# --- reserve, settle, release through the fence -----------------------------------------------


async def test_a_request_is_reserved_then_settled(ai: Connection, jenv: JobsEnv) -> None:
    lease = _lease(ai)
    settings = _settings(jenv, 1000, 5000)
    async with fenced_session(jenv.factory, lease) as fs:
        held = await budget.reserve(fs, 800, settings)
    assert held is not None
    assert held.amount == 800
    assert counters(ai, "ai_spend", owner_id=lease.owner_id, month=_month()) == (800, 0)
    async with fenced_session(jenv.factory, lease) as fs:
        assert await budget.reserve(fs, 201, settings) is None
        assert await budget.settle(fs, held, 120) is Settled.SETTLED
    async with fenced_session(jenv.factory, lease) as fs:
        assert await budget.settle(fs, held, 120) is Settled.UNCHANGED
        assert await budget.release(fs, held) is False
    assert counters(ai, "ai_spend", owner_id=lease.owner_id, month=_month()) == (0, 120)


async def test_a_refused_reservation_is_logged_without_amounts(
    ai: Connection, jenv: JobsEnv, caplog: pytest.LogCaptureFixture
) -> None:
    lease = _lease(ai)
    caplog.set_level(logging.INFO, logger="ownvitals.ai.budget")
    async with fenced_session(jenv.factory, lease) as fs:
        assert await budget.reserve(fs, 777_777, _settings(jenv, 0, 0)) is None
    record = next(r for r in caplog.records if r.getMessage() == "ai reservation refused")
    assert getattr(record, "event", None) == "ai_budget_refused"
    assert "777777" not in caplog.text
    assert str(lease.owner_id) not in caplog.text


async def test_an_overrun_is_charged_and_reported_as_a_count(
    ai: Connection, jenv: JobsEnv, caplog: pytest.LogCaptureFixture
) -> None:
    lease = _lease(ai)
    settings = _settings(jenv, 1000, 1000)
    async with fenced_session(jenv.factory, lease) as fs:
        held = await budget.reserve(fs, 900, settings)
    assert held is not None
    async with fenced_session(jenv.factory, lease) as fs:
        assert await budget.settle(fs, held, 1_234) is Settled.OVERRUN
    async with fenced_session(jenv.factory, lease) as fs:
        assert await budget.reserve(fs, 1, settings) is None
    assert counters(ai, "ai_spend", owner_id=lease.owner_id, month=_month()) == (0, 1_234)
    record = next(r for r in caplog.records if r.getMessage() == "ai cost overrun")
    assert getattr(record, "event", None) == "ai_cost_overrun"
    assert "1234" not in caplog.text


async def test_a_released_request_costs_nothing(ai: Connection, jenv: JobsEnv) -> None:
    lease = _lease(ai)
    async with fenced_session(jenv.factory, lease) as fs:
        held = await budget.reserve(fs, 500, _settings(jenv, BIG, BIG))
    assert held is not None
    async with fenced_session(jenv.factory, lease) as fs:
        assert await budget.release(fs, held) is True
    assert counters(ai, "ai_spend", owner_id=lease.owner_id, month=_month()) == (0, 0)


async def test_a_lost_lease_cannot_settle_and_the_reaper_charges_in_full(
    ai: Connection, jenv: JobsEnv
) -> None:
    """The worker's lease was taken over between reserve and settle: its settle is fenced out,
    and the reaper charges the reservation at the reserved amount."""
    lease = _lease(ai)
    async with fenced_session(jenv.factory, lease) as fs:
        held = await budget.reserve(fs, 300, _settings(jenv, BIG, BIG))
    assert held is not None
    ai.execute(text("UPDATE jobs SET lease_version = 2 WHERE id = :j"), {"j": lease.job_id})
    with pytest.raises(LeaseLost):
        async with fenced_session(jenv.factory, lease) as fs:
            await budget.settle(fs, held, 10)
    assert await budget.settle_lost(jenv.factory) == 1
    assert await budget.settle_lost(jenv.factory) == 0
    assert (reservation(ai, held.id).state, reservation(ai, held.id).actual) == ("settled", 300)


@pytest.mark.parametrize("amount", [0, -1, budget.MAX_AMOUNT + 1, 1.5, True])
async def test_reserve_refuses_an_amount_outside_its_bounds(
    ai: Connection, jenv: JobsEnv, amount: Any
) -> None:
    lease = _lease(ai)
    async with fenced_session(jenv.factory, lease) as fs:
        with pytest.raises(ValueError, match="amount"):
            await budget.reserve(fs, amount, _settings(jenv, BIG, BIG))


@pytest.mark.parametrize("actual", [-1, budget.MAX_ACTUAL + 1, 2.0, None])
async def test_settle_refuses_an_actual_outside_its_bounds(
    ai: Connection, jenv: JobsEnv, actual: Any
) -> None:
    lease = _lease(ai)
    async with fenced_session(jenv.factory, lease) as fs:
        with pytest.raises(ValueError, match="actual"):
            await budget.settle(fs, Reservation(uuid.uuid4(), 1), actual)


@pytest.mark.parametrize("limit", [0, 1001, "5"])
async def test_settle_lost_refuses_a_limit_outside_its_bounds(jenv: JobsEnv, limit: Any) -> None:
    with pytest.raises(ValueError, match="limit"):
        await budget.settle_lost(jenv.factory, limit)


# --- the user's budget ------------------------------------------------------------------------


async def test_the_month_budget_shows_the_paused_state(ai: Connection, jenv: JobsEnv) -> None:
    lease = _lease(ai)
    settings = _settings(jenv, 1000, 1000)
    now = datetime.now(UTC)
    async with user_session(jenv.factory, lease.ctx) as db:
        fresh = await budget.month_budget(db, lease.ctx, settings, now)
    assert (fresh.spent, fresh.reserved, fresh.cap, fresh.paused) == (0, 0, 1000, False)
    assert fresh.message() is None
    async with fenced_session(jenv.factory, lease) as fs:
        held = await budget.reserve(fs, 1000, settings)
    assert held is not None
    async with user_session(jenv.factory, lease.ctx) as db:
        full = await budget.month_budget(db, lease.ctx, settings, now)
        # Another user's view of the same month: their own row only (forced RLS).
        other = make_context(uuid.uuid4())
    async with user_session(jenv.factory, other) as db:
        theirs = await budget.month_budget(db, other, settings, now)
    assert (full.reserved, full.paused) == (1000, True)
    assert full.message() == budget.paused_message(budget.resets_on(now))
    assert (theirs.reserved, theirs.spent, theirs.paused) == (0, 0, False)


def test_caps_of_zero_mean_paused_from_the_start() -> None:
    month = budget.MonthBudget(date(2026, 11, 1), 0, 0, 0, date(2026, 12, 1))
    assert month.paused
    assert month.message() == (
        "This month's AI budget for your account is used up. It resets on 1 December 2026."
    )


@pytest.mark.parametrize(
    ("now", "start", "reset"),
    [
        (datetime(2026, 1, 31, 23, 59, tzinfo=UTC), date(2026, 1, 1), date(2026, 2, 1)),
        (datetime(2026, 12, 15, tzinfo=UTC), date(2026, 12, 1), date(2027, 1, 1)),
        # 00:30 on 1 March in UTC+2 is still February in UTC.
        (
            datetime(2026, 3, 1, 0, 30, tzinfo=timezone(timedelta(hours=2))),
            date(2026, 2, 1),
            date(2026, 3, 1),
        ),
    ],
)
def test_the_month_is_the_utc_calendar_month(now: datetime, start: date, reset: date) -> None:
    assert budget.month_start(now) == start
    assert budget.resets_on(now) == reset


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        budget.month_start(datetime(2026, 1, 1))


# --- release or charge ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sent", "status", "release"),
    [
        (False, None, True),  # the connection failed before the request was sent
        *[(True, s, True) for s in (400, 401, 403, 404, 413, 429, 529)],
        (True, None, False),  # a timeout or a broken connection after sending
        *[(True, s, False) for s in (200, 408, 409, 422, 500, 502, 503, 504)],
    ],
)
def test_only_an_unprocessed_request_is_released(
    sent: bool, status: int | None, release: bool
) -> None:
    assert budget.may_release(sent=sent, status=status) is release


# --- settings and the worker ------------------------------------------------------------------


def test_the_caps_default_to_zero() -> None:
    fields = Settings.model_fields
    assert fields["ai_user_monthly_cap_microusd"].default == 0
    assert fields["ai_global_monthly_cap_microusd"].default == 0


@pytest.mark.parametrize(("user_cap", "global_cap"), [(1, 0), (-1, 10), (10, -1)])
def test_the_settings_refuse_a_user_cap_above_the_global_one(
    app_database_url: str, user_cap: int, global_cap: int
) -> None:
    with pytest.raises(ValidationError):
        settings_for(
            app_database_url,
            ai_user_monthly_cap_microusd=user_cap,
            ai_global_monthly_cap_microusd=global_cap,
        )


@pytest.mark.parametrize("ai_on", [True, False])
async def test_the_worker_s_maintenance_runs_the_reaper_only_with_ai_on(
    jenv: JobsEnv, monkeypatch: pytest.MonkeyPatch, ai_on: bool
) -> None:
    from ownvitals.worker import loop

    calls: list[str] = []

    async def reap(_: object) -> int:
        calls.append("reap")
        return 0

    async def settle_lost(_: object) -> int:
        calls.append("settle_lost")
        return 0

    monkeypatch.setattr(loop.queue, "reap", reap)
    monkeypatch.setattr(loop.budget, "settle_lost", settle_lost)
    worker = loop.Worker(jenv.settings.model_copy(update={"ai_insights": ai_on}), jenv.engine)
    monkeypatch.setattr(worker, "_startup_passed", lambda: True)
    await worker.maintain()
    assert calls == (["reap", "settle_lost"] if ai_on else ["reap"])
