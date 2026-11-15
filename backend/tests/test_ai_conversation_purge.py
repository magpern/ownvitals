"""The AI conversation retention (ADR 0014 section 5; #395): ``ai_purge_expired`` in 50-ai.sql,
``ownvitals.ai.conversation_retention`` and the worker's hourly purge tick.

Synthetic data only: owners are random UUIDs with ``example.test`` addresses, and every message
is the fixed text ``synthetic message``, no health data.

- conversations last updated past the retention go with their messages, for every owner, oldest
  first and at most the limit per call; younger ones, and other owners' young ones, stay; the
  boundary is the retention to the minute on either side;
- the purge takes no owner: a caller's user context neither widens nor narrows it, and the app
  role on its own (forced RLS) still reaches only its own user's conversations;
- a conversation a reply touched while the purge waited for its row lock stays;
- a retention outside 30 to 90 days, and a limit out of bounds, are refused by the function and
  by the Python layer;
- the Python layer works in batches, does nothing when 50-ai.sql is not installed, and the
  worker's purge tick runs it whether or not ``ai_insights`` is on, after the other steps and
  even when one of them fails.

The function's privileges, pins and drift are covered with the family in
``test_ai_functions.py``; the role's grants by guard 2.
"""

import logging
import threading
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
from ownvitals.ai import conversation_retention
from ownvitals.ai.conversation_retention import purge_expired_conversations
from ownvitals.worker import upkeep

PURGE = "SELECT ai_purge_expired(:days, :n)"


@pytest.fixture(name="history")
def history_fixture(admin: Connection) -> Iterator[Connection]:
    """The superuser connection on an empty conversation history."""
    admin.execute(text("TRUNCATE ai_conversations CASCADE"))
    yield admin
    admin.execute(text("TRUNCATE ai_conversations CASCADE"))


def plant(su: Connection, owner: uuid.UUID, age: str, messages: int = 2) -> uuid.UUID:
    """One synthetic conversation of ``owner`` last updated ``age`` ago (a Postgres interval),
    created a year before that, with ``messages`` synthetic messages."""
    ensure_user(su, owner)
    conversation = su.execute(
        text(
            "INSERT INTO ai_conversations (id, owner_id, created_at, updated_at) VALUES "
            "(gen_random_uuid(), :owner, now() - CAST(:age AS interval) - interval '1 year', "
            "now() - CAST(:age AS interval)) RETURNING id"
        ),
        {"owner": owner, "age": age},
    ).scalar_one()
    for i in range(messages):
        su.execute(
            text(
                "INSERT INTO ai_messages (owner_id, conversation_id, role, content) "
                "VALUES (:owner, :c, :role, 'synthetic message')"
            ),
            {"owner": owner, "c": conversation, "role": ("user", "assistant")[i % 2]},
        )
    return conversation


def left(su: Connection) -> set[uuid.UUID]:
    return set(su.execute(text("SELECT id FROM ai_conversations")).scalars())


def messages_of(su: Connection) -> set[uuid.UUID]:
    return set(su.execute(text("SELECT DISTINCT conversation_id FROM ai_messages")).scalars())


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def test_expired_conversations_go_with_their_messages_and_the_rest_stays(
    history: Connection, app_database_url: str
) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    plant(history, alice, "91 days")
    plant(history, bob, "90 days 1 minute")
    young = {
        plant(history, alice, "90 days -1 minute"),
        plant(history, bob, "1 day"),
        plant(history, bob, "0 seconds"),
    }
    with app_engine(app_database_url) as engine:
        assert call(engine, None, PURGE, days=90, n=100) == 2
        assert call(engine, None, PURGE, days=90, n=100) == 0
    assert left(history) == young
    assert messages_of(history) == young
    assert history.execute(text("SELECT count(*) FROM ai_messages")).scalar_one() == 6


def test_two_users_the_purge_never_takes_the_other_user_s_young_rows(
    history: Connection, app_database_url: str
) -> None:
    """Alice's expired history goes; Bob's, all young, stays whole. A caller's ``app.user_id``
    changes nothing about what goes, and the app role itself (forced RLS) still reaches only
    the context user's own conversations."""
    alice, bob = uuid.uuid4(), uuid.uuid4()
    alice_old = plant(history, alice, "120 days")
    alice_young = plant(history, alice, "10 days")
    bob_young = {plant(history, bob, "89 days"), plant(history, bob, "1 hour")}
    with app_engine(app_database_url) as engine:
        assert call(engine, bob, PURGE, days=90, n=100) == 1
        assert left(history) == {alice_young, *bob_young}
        assert alice_old not in messages_of(history)
        # The app role under Alice's context: its own rows only, never Bob's.
        seen = "SELECT count(*) FROM ai_conversations"
        assert call(engine, alice, seen) == 1
        assert call(engine, None, seen) == 0
        mine = "WITH d AS (DELETE FROM ai_conversations RETURNING 1) SELECT count(*) FROM d"
        assert call(engine, alice, mine) == 1
    assert left(history) == bob_young
    assert messages_of(history) == bob_young


def test_a_shorter_retention_takes_more_and_the_limit_takes_the_oldest_first(
    history: Connection, app_database_url: str
) -> None:
    owner = uuid.uuid4()
    oldest = plant(history, owner, "200 days")
    older = plant(history, owner, "40 days")
    kept = plant(history, owner, "29 days")
    with app_engine(app_database_url) as engine:
        assert call(engine, None, PURGE, days=30, n=1) == 1
        assert left(history) == {older, kept}
        assert call(engine, None, PURGE, days=90, n=1) == 0
        assert call(engine, None, PURGE, days=30, n=1) == 1
        assert call(engine, None, PURGE, days=30, n=1) == 0
    assert left(history) == {kept}
    assert oldest not in messages_of(history)


def test_a_conversation_touched_while_the_purge_waits_stays(
    history: Connection, app_database_url: str
) -> None:
    """The age is checked again on the row the DELETE locks: a reply that holds the lock and
    touches ``updated_at`` (``ownvitals.ai.assistant``) keeps its conversation."""
    owner = uuid.uuid4()
    busy = plant(history, owner, "100 days")
    idle = plant(history, owner, "100 days")
    result: dict[str, Any] = {}
    with app_engine(app_database_url) as engine, engine.connect() as reply:
        reply.execute(text("SELECT set_config('app.user_id', :o, true)"), {"o": str(owner)})
        reply.execute(text("SELECT id FROM ai_conversations WHERE id = :c FOR UPDATE"), {"c": busy})
        reply.execute(
            text("UPDATE ai_conversations SET updated_at = now() WHERE id = :c"), {"c": busy}
        )

        def purge() -> None:
            result["n"] = call(engine, None, PURGE, days=90, n=100)

        worker = threading.Thread(target=purge)
        worker.start()
        waiting = text(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND query LIKE '%ai_purge_expired%'"
        )
        for _ in range(500):
            if history.execute(waiting).scalar_one():
                break
            threading.Event().wait(0.01)
        else:
            pytest.fail("the purge never waited for the row lock")
        reply.commit()
        worker.join(10)
    assert result == {"n": 1}
    assert left(history) == {busy}
    assert idle not in messages_of(history)


@pytest.mark.parametrize(
    ("days", "limit"),
    [
        (29, 10),
        (0, 10),
        (-90, 10),
        (91, 10),
        (365, 10),
        (None, 10),
        (90, 0),
        (90, 10_001),
        (90, None),
    ],
)
def test_the_function_refuses_a_retention_or_a_limit_out_of_bounds(
    history: Connection, app_database_url: str, days: Any, limit: Any
) -> None:
    kept = plant(history, uuid.uuid4(), "100 days")
    with app_engine(app_database_url) as engine, pytest.raises(DBAPIError) as refused:
        call(engine, None, PURGE, days=days, n=limit)
    assert _sqlstate(refused.value) == "22023"
    assert left(history) == {kept}


async def test_the_python_layer_purges_in_batches(history: Connection, jenv: JobsEnv) -> None:
    owner = uuid.uuid4()
    for _ in range(5):
        plant(history, owner, "100 days", messages=1)
    young = plant(history, owner, "80 days")
    assert await purge_expired_conversations(jenv.factory, 90, limit=2, batches=2) == 4
    assert await purge_expired_conversations(jenv.factory, 90, limit=2, batches=2) == 1
    assert await purge_expired_conversations(jenv.factory, 90, limit=2, batches=2) == 0
    assert left(history) == {young}
    assert messages_of(history) == {young}


@pytest.mark.parametrize(
    "arguments",
    [
        {"days": 29},
        {"days": 91},
        {"days": True},
        {"days": 90.0},
        {"days": 90, "limit": 0},
        {"days": 90, "limit": 10_001},
        {"days": 90, "batches": 0},
        {"days": 90, "batches": 101},
    ],
)
async def test_the_python_layer_refuses_arguments_out_of_bounds(
    history: Connection, jenv: JobsEnv, arguments: dict[str, Any]
) -> None:
    kept = plant(history, uuid.uuid4(), "100 days")
    with pytest.raises(ValueError, match="must be an integer"):
        await purge_expired_conversations(jenv.factory, **arguments)
    assert left(history) == {kept}


async def test_nothing_is_called_when_50_ai_sql_is_not_installed(
    history: Connection, jenv: JobsEnv, queue_schema: str
) -> None:
    kept = plant(history, uuid.uuid4(), "100 days")
    drop_ai_functions(queue_schema)
    try:
        assert await purge_expired_conversations(jenv.factory, 90) == 0
        assert left(history) == {kept}
    finally:
        run_script(queue_schema, AI_SCRIPT.read_text(encoding="utf-8"))
    assert await purge_expired_conversations(jenv.factory, 90) == 1


@pytest.mark.parametrize("days", [29, 0, 91, 365])
def test_the_setting_is_90_days_and_stays_from_30_to_90(app_database_url: str, days: int) -> None:
    assert settings_for(app_database_url).ai_conversation_retention_days == 90
    assert (conversation_retention.MIN_DAYS, conversation_retention.MAX_DAYS) == (30, 90)
    shorter = settings_for(app_database_url, ai_conversation_retention_days=30)
    assert shorter.ai_conversation_retention_days == 30
    with pytest.raises(ValidationError, match="ai_conversation_retention_days"):
        settings_for(app_database_url, ai_conversation_retention_days=days)


@pytest.mark.parametrize("ai_on", [True, False])
async def test_the_worker_s_purge_tick_runs_the_conversation_purge_with_ai_on_or_off(
    history: Connection, jenv: JobsEnv, ai_on: bool
) -> None:
    owner = uuid.uuid4()
    plant(history, owner, "100 days")
    young = plant(history, owner, "1 day")
    settings = jenv.settings.model_copy(update={"ai_insights": ai_on})
    await upkeep.purge(jenv.factory, settings)
    assert left(history) == {young}


async def test_the_tick_passes_the_retention_setting(history: Connection, jenv: JobsEnv) -> None:
    owner = uuid.uuid4()
    plant(history, owner, "40 days")
    kept = plant(history, owner, "20 days")
    await upkeep.purge(jenv.factory, jenv.settings)
    assert len(left(history)) == 2
    settings = jenv.settings.model_copy(update={"ai_conversation_retention_days": 30})
    await upkeep.purge(jenv.factory, settings)
    assert left(history) == {kept}


async def test_a_failing_step_does_not_stop_the_conversation_purge(
    history: Connection,
    jenv: JobsEnv,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    young = plant(history, uuid.uuid4(), "1 day")
    plant(history, uuid.uuid4(), "100 days")

    async def broken(*_: object, **__: object) -> int:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(upkeep, "purge_expired_audit", broken)
    with caplog.at_level(logging.INFO, logger="ownvitals.worker.loop"):
        await upkeep.purge(jenv.factory, jenv.settings)
    assert left(history) == {young}
    counted = [r for r in caplog.records if getattr(r, "step", None) == "ai conversation purge"]
    assert [getattr(r, "count", None) for r in counted] == [1]

    monkeypatch.setattr(upkeep, "purge_expired_conversations", broken)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ownvitals.worker.loop"):
        await upkeep.purge(jenv.factory, jenv.settings)
    failed = [r for r in caplog.records if r.getMessage() == "job maintenance failed"]
    assert ("ai conversation purge", "RuntimeError") in [
        (getattr(r, "step", None), getattr(r, "error_type", None)) for r in failed
    ]
