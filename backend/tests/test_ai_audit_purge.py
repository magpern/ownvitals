"""The AI audit log's 12-month retention (ADR 0014 section 6; #331): ``ai_audit_purge`` in
50-ai.sql, ``ownvitals.ai.audit_retention`` and the worker's hourly purge tick.

Synthetic data only: owners are random UUIDs with ``example.test`` addresses, and the audit rows
hold made-up counts and fingerprints, no content.

- rows past the retention go, for every owner, oldest first and at most the limit per call;
  younger rows and the other AI tables stay;
- a retention below 12 months (or above 120), and a limit out of bounds, are refused by the
  function and by the Python layer, so no caller can shorten the retention;
- the Python layer works in batches, does nothing when 50-ai.sql is not installed, and the
  worker's purge tick runs it whether or not ``ai_insights`` is on, after the auth purge and
  even when that fails.

The function's privileges, pins and drift are covered with the family in
``test_ai_functions.py``; the role's grants (DELETE on ``ai_audit`` only) by guard 2.
"""

import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from aitools import app_engine, call
from dbtools import AI_SCRIPT, drop_ai_functions
from jobcore import JobsEnv, jenv_fixture, settings_for  # noqa: F401 - fixtures
from jobtools import (  # noqa: F401 - fixtures
    admin_fixture,
    ensure_user,
    queue_schema_fixture,
    run_script,
)
from ownvitals.ai import audit_retention
from ownvitals.ai.audit_retention import purge_expired_audit
from ownvitals.worker import upkeep

PURGE = "SELECT ai_audit_purge(:months, :n)"


@pytest.fixture(name="audit")
def audit_fixture(admin: Connection) -> Iterator[Connection]:
    """The superuser connection on an empty ``ai_audit``."""
    admin.execute(text("TRUNCATE ai_audit"))
    yield admin
    admin.execute(text("TRUNCATE ai_audit"))


def plant(su: Connection, owner: uuid.UUID, age: str) -> uuid.UUID:
    """One synthetic audit row of ``owner`` written ``age`` ago (a Postgres interval)."""
    ensure_user(su, owner)
    return su.execute(
        text(
            "INSERT INTO ai_audit (owner_id, kind, model, prompt_version, toolset_version, "
            "cost_microusd, reservation_id, latency_ms, attempts, status, settlement, "
            "request_hmac, created_at) VALUES (:owner, 'chat', 'synthetic-model', '1', '1', 0, "
            ":reservation, 1, 1, 'completed', 'settled', :hmac, now() - CAST(:age AS interval)) "
            "RETURNING id"
        ),
        {"owner": owner, "reservation": uuid.uuid4(), "hmac": bytes(32), "age": age},
    ).scalar_one()


def left(su: Connection) -> set[uuid.UUID]:
    return set(su.execute(text("SELECT id FROM ai_audit")).scalars())


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def test_rows_past_retention_go_for_every_owner_and_the_rest_stays(
    audit: Connection, app_database_url: str
) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    old = {plant(audit, alice, "13 months"), plant(audit, bob, "12 months 1 day")}
    young = {
        plant(audit, alice, "12 months -1 day"),
        plant(audit, bob, "1 day"),
        plant(audit, bob, "0 seconds"),
    }
    reservations = audit.execute(text("SELECT count(*) FROM ai_reservations")).scalar_one()
    with app_engine(app_database_url) as engine:
        assert call(engine, None, PURGE, months=12, n=100) == 2
        assert call(engine, None, PURGE, months=12, n=100) == 0
    assert left(audit) == young
    assert not old & left(audit)
    assert audit.execute(text("SELECT count(*) FROM ai_reservations")).scalar_one() == reservations


def test_a_longer_retention_keeps_more_and_the_limit_takes_the_oldest_first(
    audit: Connection, app_database_url: str
) -> None:
    owner = uuid.uuid4()
    oldest = plant(audit, owner, "30 months")
    older = plant(audit, owner, "25 months")
    kept = plant(audit, owner, "13 months")
    with app_engine(app_database_url) as engine:
        assert call(engine, None, PURGE, months=24, n=1) == 1
        assert left(audit) == {older, kept}
        assert call(engine, None, PURGE, months=24, n=1) == 1
        assert call(engine, None, PURGE, months=24, n=1) == 0
    assert left(audit) == {kept}
    assert oldest not in left(audit)


def test_a_user_context_changes_nothing_about_what_goes(
    audit: Connection, app_database_url: str
) -> None:
    """The function takes no owner: a caller's ``app.user_id`` neither widens nor narrows it."""
    alice, bob = uuid.uuid4(), uuid.uuid4()
    plant(audit, alice, "13 months")
    plant(audit, bob, "13 months")
    young = plant(audit, alice, "1 month")
    with app_engine(app_database_url) as engine:
        assert call(engine, alice, PURGE, months=12, n=100) == 2
    assert left(audit) == {young}


@pytest.mark.parametrize(
    ("months", "limit"),
    [(11, 10), (0, 10), (-12, 10), (121, 10), (None, 10), (12, 0), (12, 10_001), (12, None)],
)
def test_the_function_refuses_a_shorter_retention_or_a_limit_out_of_bounds(
    audit: Connection, app_database_url: str, months: Any, limit: Any
) -> None:
    kept = plant(audit, uuid.uuid4(), "13 months")
    with app_engine(app_database_url) as engine, pytest.raises(DBAPIError) as refused:
        call(engine, None, PURGE, months=months, n=limit)
    assert _sqlstate(refused.value) == "22023"
    assert left(audit) == {kept}


def test_the_app_role_still_cannot_delete_audit_rows_itself(
    audit: Connection, app_database_url: str
) -> None:
    owner = uuid.uuid4()
    kept = plant(audit, owner, "13 months")
    with app_engine(app_database_url) as engine, pytest.raises(DBAPIError) as refused:
        call(engine, owner, "WITH d AS (DELETE FROM ai_audit RETURNING 1) SELECT count(*) FROM d")
    assert _sqlstate(refused.value) == "42501"
    assert left(audit) == {kept}


async def test_the_python_layer_purges_in_batches(audit: Connection, jenv: JobsEnv) -> None:
    owner = uuid.uuid4()
    for _ in range(5):
        plant(audit, owner, "13 months")
    young = plant(audit, owner, "11 months")
    assert await purge_expired_audit(jenv.factory, 12, limit=2, batches=2) == 4
    assert await purge_expired_audit(jenv.factory, 12, limit=2, batches=2) == 1
    assert await purge_expired_audit(jenv.factory, 12, limit=2, batches=2) == 0
    assert left(audit) == {young}


@pytest.mark.parametrize(
    "arguments",
    [
        {"months": 11},
        {"months": 121},
        {"months": True},
        {"months": 12.0},
        {"months": 12, "limit": 0},
        {"months": 12, "limit": 10_001},
        {"months": 12, "batches": 0},
        {"months": 12, "batches": 101},
    ],
)
async def test_the_python_layer_refuses_arguments_out_of_bounds(
    audit: Connection, jenv: JobsEnv, arguments: dict[str, Any]
) -> None:
    kept = plant(audit, uuid.uuid4(), "13 months")
    with pytest.raises(ValueError, match="must be an integer"):
        await purge_expired_audit(jenv.factory, **arguments)
    assert left(audit) == {kept}


async def test_nothing_is_called_when_50_ai_sql_is_not_installed(
    audit: Connection, jenv: JobsEnv, queue_schema: str
) -> None:
    kept = plant(audit, uuid.uuid4(), "13 months")
    drop_ai_functions(queue_schema)
    try:
        assert await purge_expired_audit(jenv.factory, 12) == 0
        assert left(audit) == {kept}
    finally:
        run_script(queue_schema, AI_SCRIPT.read_text(encoding="utf-8"))
    assert await purge_expired_audit(jenv.factory, 12) == 1


@pytest.mark.parametrize("months", [11, 0, 121])
def test_the_setting_is_12_months_and_never_fewer(app_database_url: str, months: int) -> None:
    assert settings_for(app_database_url).ai_audit_retention_months == 12
    assert audit_retention.MIN_MONTHS == 12
    assert settings_for(app_database_url, ai_audit_retention_months=24).ai_audit_retention_months
    with pytest.raises(ValidationError, match="ai_audit_retention_months"):
        settings_for(app_database_url, ai_audit_retention_months=months)


@pytest.mark.parametrize("ai_on", [True, False])
async def test_the_worker_s_purge_tick_runs_the_audit_purge_with_ai_on_or_off(
    audit: Connection, jenv: JobsEnv, ai_on: bool
) -> None:
    owner = uuid.uuid4()
    plant(audit, owner, "13 months")
    young = plant(audit, owner, "1 month")
    settings = jenv.settings.model_copy(update={"ai_insights": ai_on})
    await upkeep.purge(jenv.factory, settings)
    assert left(audit) == {young}


async def test_the_tick_passes_the_retention_setting(audit: Connection, jenv: JobsEnv) -> None:
    owner = uuid.uuid4()
    older = plant(audit, owner, "13 months")
    settings = jenv.settings.model_copy(update={"ai_audit_retention_months": 24})
    await upkeep.purge(jenv.factory, settings)
    assert left(audit) == {older}


async def test_a_failing_step_does_not_stop_the_other(
    audit: Connection,
    jenv: JobsEnv,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    young = plant(audit, uuid.uuid4(), "1 month")
    plant(audit, uuid.uuid4(), "13 months")

    async def broken_auth(*_: object) -> dict[str, int]:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(upkeep, "purge_expired", broken_auth)
    with caplog.at_level(logging.INFO, logger="ownvitals.worker.loop"):
        await upkeep.purge(jenv.factory, jenv.settings)
    assert left(audit) == {young}
    assert any(r.getMessage() == "auth purge failed" for r in caplog.records)
    counted = [r for r in caplog.records if getattr(r, "step", None) == "ai audit purge"]
    assert [getattr(r, "count", None) for r in counted] == [1]

    async def broken_audit(*_: object) -> int:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(upkeep, "purge_expired_audit", broken_audit)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ownvitals.worker.loop"):
        await upkeep.purge(jenv.factory, jenv.settings)
    failed = [r for r in caplog.records if r.getMessage() == "job maintenance failed"]
    assert [(getattr(r, "step", None), getattr(r, "error_type", None)) for r in failed] == [
        ("ai audit purge", "RuntimeError")
    ]
