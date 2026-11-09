"""The rule evaluation against the database (#187): a synthetic year through ingest, the normaliser
and the daily pass into episodes; idempotence; origin and notification; re-derivation after a data
revision; the experimental switch; Q1; the purge race, the lock and concurrent passes; other
rules' rows; the worker context and the chain from the daily pass.

Setup rows go through the ingest code, the normaliser and the daily pass; the evaluation runs as
``ownvitals_app``. **Synthetic** users, groups and measures only (``synthetic_`` subjects of the
fixtures, made-up values); no real account or measurement.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobcore import JobsEnv, jenv_fixture  # noqa: F401 - fixtures
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.config import Settings
from ownvitals.connections.purge import purge_imported
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.db.queue_locks import NORMALISATION
from ownvitals.derived.daily_db import daily_owner
from ownvitals.derived.daily_job import KIND as DAILY_KIND
from ownvitals.derived.daily_job import register_daily_kind
from ownvitals.derived.evaluate_job import (
    KIND,
    EvaluateInput,
    enqueue_evaluate,
    register_evaluate_kind,
)
from ownvitals.derived.findings_store import Outcome, evaluate_owner
from ownvitals.derived.normalize_db import normalise_owner
from ownvitals.ingest.rows import soft_delete_measure
from ownvitals.jobs import KINDS, KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.worker.kinds import worker_registry
from test_derived_normalize_db import Env as NormalizeEnv
from test_derived_normalize_db import group

pytestmark = pytest.mark.usefixtures("jenv")

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
TODAY = date(2026, 11, 1)  # NOW in Stockholm
DAY = 86_400
FIRST = int(datetime(2025, 11, 1, 6, 30, tzinfo=UTC).timestamp())  # 07:30 in Stockholm
YEAR = 365  # days 0 .. 364; day 364 is 2026-10-31, the day before TODAY
C1X_DAYS = (60, 61, 62)  # 49, 48, 49 bpm: C1x (experimental), never C1
C1_PAST = (150, 151, 152)  # 71, 72, 71 bpm: a C1 episode in the history (backfill)
C1_RECENT = (362, 363, 364)  # 71, 72, 71 bpm up to yesterday: a live C1 episode, still active
SNAPSHOT = (
    "SELECT id, rule_id, rule_version, tier, severity, confidence, origin, metric_keys, subject, "
    "window_start, window_end, reference_day, evidence, data_quality, status, first_seen, "
    "last_seen, ended_at, notify_state, input_revision, input_fingerprint FROM findings "
    "WHERE owner_id = :o ORDER BY rule_id, subject, window_start"
)


def day_of(index: int) -> date:
    return date(2025, 11, 1) + timedelta(days=index)


def at(day: date) -> datetime:
    """The timestamp the pass stores for an evaluation day: 00:00 UTC."""
    return datetime.combine(day, datetime.min.time(), UTC)


def pulse_on(index: int) -> int:
    special = {60: 49, 61: 48, 62: 49, 150: 71, 151: 72, 152: 71, 362: 71, 363: 72, 364: 71}
    return special.get(index, 60 if index % 2 else 62)


def year_groups(days: range = range(YEAR)) -> list[Any]:
    """A synthetic year: one morning group a day with a weight and a heart pulse."""
    return [
        group(
            1000 + i,
            FIRST + i * DAY,
            (1, 80_000 + (100 if i % 2 else -100), -3, None),
            (11, pulse_on(i), 0, None),
        )
        for i in days
    ]


class Env(NormalizeEnv):
    def owner(self) -> tuple[UserContext, uuid.UUID]:
        ctx, connection = super().owner()
        self.connection_status(connection, "connected")
        return ctx, connection

    def connection_status(self, connection: uuid.UUID, status: str) -> None:
        """A synthetic credential (random bytes, no key behind them) so ``connected`` passes the
        table's checks; nothing here decrypts it."""
        self.jenv.sql(
            "UPDATE connections SET status = :s, secret = '\\x00'::bytea, "
            "secret_nonce = decode(repeat('00', 12), 'hex'), "
            "wrapped_dek = decode(repeat('00', 48), 'hex'), "
            "dek_nonce = decode(repeat('00', 12), 'hex'), key_id = 'kek-synthetic' WHERE id = :c",
            s=status,
            c=connection,
        )

    def settings(self, *, experimental: bool = False) -> Settings:
        return self.jenv.settings.model_copy(update={"experimental_findings": experimental})

    async def derive(self, ctx: UserContext) -> None:
        await normalise_owner(self.opener(ctx), ctx.user_id, now=NOW)
        await daily_owner(self.opener(ctx), ctx.user_id)

    async def evaluate(
        self, ctx: UserContext, *, experimental: bool = False, now: datetime = NOW, **kw: Any
    ) -> Outcome:
        return await evaluate_owner(
            self.opener(ctx),
            ctx.user_id,
            settings=self.settings(experimental=experimental),
            now=now,
            **kw,
        )

    async def all_of(self, ctx: UserContext, **kw: Any) -> Outcome:
        await self.derive(ctx)
        return await self.evaluate(ctx, **kw)

    async def found(self, ctx: UserContext) -> list[Any]:
        async with user_session(self.factory, ctx) as db:
            return list((await db.execute(text(SNAPSHOT), {"o": ctx.user_id})).all())


@pytest.fixture
def env(jenv: JobsEnv) -> Env:
    return Env(jenv)


async def a_year(env: Env) -> tuple[UserContext, uuid.UUID]:
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, *year_groups())
    return ctx, connection


def summary(rows: list[Any]) -> list[tuple[Any, ...]]:
    return [
        (r.rule_id, r.subject, r.reference_day, r.window_end, r.status, r.origin, r.notify_state)
        for r in rows
    ]


# --- a synthetic year: the expected episodes ----------------------------------------------------


async def test_a_synthetic_year_gives_the_expected_episodes(env: Env) -> None:
    ctx, _ = await a_year(env)
    outcome = await env.all_of(ctx)
    assert (outcome.status, outcome.inserted, outcome.pending) == ("written", 2, 1)
    assert outcome.evaluated is not None
    assert outcome.evaluated["C1"] == YEAR
    assert "C1x" not in outcome.evaluated  # experimental: not computed without the switch
    rows = await env.found(ctx)
    assert summary(rows) == [
        # C1 in the history: the run's first day is the reference; it ended on the next day.
        ("C1", "heart_pulse", day_of(150), day_of(152), "ended", "backfill", "none"),
        # C1 now: the window ends yesterday, so it is live and marked to notify.
        ("C1", "heart_pulse", day_of(362), day_of(364), "active", "live", "pending"),
    ]
    past, recent = rows
    assert (past.window_start, past.ended_at) == (day_of(150), at(day_of(153)))
    # first_seen and last_seen: the evaluations that opened and last extended it (day 152).
    assert (past.first_seen, past.last_seen) == (at(day_of(152)), at(day_of(152)))
    assert recent.ended_at is None
    for row in rows:
        assert (row.tier, row.severity, row.metric_keys) == ("trend", "attention", ["heart_pulse"])
        assert row.rule_version == "rules/1.1"
        assert row.confidence == "medium"  # a run of 3 (R: under twice the minimum)
        assert row.evidence["run_days"] == 3
        assert row.evidence["trigger_groups"] == [1000 + i for i in range(150, 153)] or (
            row.evidence["trigger_groups"] == [1000 + i for i in C1_RECENT]
        )
        assert row.data_quality["algo_version"]
        assert len(bytes(row.input_fingerprint)) == 32
    assert len({(r.input_revision, bytes(r.input_fingerprint)) for r in rows}) == 1


async def test_a_second_run_changes_nothing(env: Env) -> None:
    ctx, _ = await a_year(env)
    assert (await env.all_of(ctx)).status == "written"
    first = await env.found(ctx)
    again = await env.evaluate(ctx)
    assert (again.status, again.inserted, again.updated, again.ended, again.deleted) == (
        "current", 0, 0, 0, 0,
    )  # fmt: skip
    assert await env.found(ctx) == first


async def test_a_stored_row_that_differs_is_repaired_and_keeps_its_identity(env: Env) -> None:
    ctx, _ = await a_year(env)
    await env.all_of(ctx)
    first = await env.found(ctx)
    env.jenv.sql(
        "UPDATE findings SET confidence = 'high', evidence = '{}'::jsonb WHERE owner_id = :o",
        o=ctx.user_id,
    )
    outcome = await env.evaluate(ctx)
    assert (outcome.updated, outcome.ended, outcome.inserted, outcome.deleted) == (1, 1, 0, 0)
    assert await env.found(ctx) == first


# --- origin and notification (ADR 0010 §4, PLAN §7.1 step 7) -----------------------------------


async def test_backfilled_history_is_backfill_and_never_notifies(env: Env) -> None:
    """Only the history: the latest data is 30 days old, so every episode is backfill."""
    ctx, _ = await a_year(env)
    outcome = await env.all_of(ctx, now=NOW + timedelta(days=30))
    assert outcome.pending == 0
    rows = await env.found(ctx)
    assert {(r.origin, r.notify_state) for r in rows} == {("backfill", "none")}
    assert len(rows) == 2


async def test_origin_is_computed_once_at_the_first_write(env: Env) -> None:
    ctx, _ = await a_year(env)
    await env.all_of(ctx)
    live = next(r for r in await env.found(ctx) if r.status == "active")
    # A month later the same episode is re-derived: it keeps its id, origin and first_seen,
    # and is not marked to notify again.
    later = await env.evaluate(ctx, now=NOW + timedelta(days=30))
    assert later.inserted == 0
    again = next(r for r in await env.found(ctx) if r.status == "active")
    assert (again.id, again.origin, again.first_seen, again.notify_state) == (
        live.id, "live", live.first_seen, "pending",
    )  # fmt: skip


async def test_a_notified_episode_keeps_its_notification_state(env: Env) -> None:
    ctx, connection = await a_year(env)
    await env.all_of(ctx)
    env.jenv.sql(
        "UPDATE findings SET notify_state = 'sent', notified_at = now() WHERE owner_id = :o "
        "AND status = 'active'",
        o=ctx.user_id,
    )
    # Today's reading continues the run: the episode is updated, never re-notified.
    today = group(1000 + YEAR, FIRST + YEAR * DAY, (1, 80_000, -3, None), (11, 72, 0, None))
    await env.ingest(ctx, connection, today)
    await env.derive(ctx)
    outcome = await env.evaluate(ctx)
    assert (outcome.updated, outcome.inserted, outcome.pending) == (1, 0, 0)
    active = next(r for r in await env.found(ctx) if r.status == "active")
    assert active.notify_state == "sent"


# --- re-derivation after a data revision (PLAN §5) ----------------------------------------------


async def test_a_soft_delete_re_derives_the_episodes(env: Env) -> None:
    ctx, connection = await a_year(env)
    await env.all_of(ctx)
    before = {r.reference_day: r for r in await env.found(ctx)}
    async with user_session(env.factory, ctx) as db:  # breaks the past run: 150 and 152 alone
        assert await soft_delete_measure(db, ctx.user_id, connection, 1151)
    outcome = await env.all_of(ctx)
    assert (outcome.deleted, outcome.inserted) == (1, 0)
    after = await env.found(ctx)
    assert [r.reference_day for r in after] == [day_of(362)]
    kept = after[0]
    assert (kept.id, kept.origin, kept.first_seen) == (
        before[day_of(362)].id, "live", before[day_of(362)].first_seen,
    )  # fmt: skip


async def test_an_edit_that_keeps_the_reference_day_updates_in_place(env: Env) -> None:
    ctx, connection = await a_year(env)
    await env.all_of(ctx)
    before = next(r for r in await env.found(ctx) if r.status == "active")
    edited = group(1363, FIRST + 363 * DAY, (1, 80_100, -3, None), (11, 75, 0, None))
    await env.ingest(ctx, connection, edited)
    outcome = await env.all_of(ctx)
    assert (outcome.updated, outcome.inserted, outcome.deleted) == (1, 0, 0)
    after = next(r for r in await env.found(ctx) if r.status == "active")
    assert after.id == before.id
    assert after.evidence["values"] == [71.0, 75.0, 71.0]
    assert after.notify_state == "pending"


# --- the experimental tier (D9, PLAN §7.2) -------------------------------------------------------


def experimental_rows(env: Env, owner: uuid.UUID) -> int:
    return int(
        env.jenv.sql(
            "SELECT count(*) FROM findings WHERE owner_id = :o AND (tier = 'experimental' "
            "OR rule_id IN ('B1', 'B3', 'C1x'))",
            o=owner,
        ).scalar_one()
    )


async def test_without_the_switch_no_experimental_row_exists(env: Env) -> None:
    mine, my_connection = await a_year(env)
    other, other_connection = env.owner()
    await env.ingest(other, other_connection, *year_groups())
    await env.all_of(mine, experimental=True)  # the switch is on for this pass only
    await env.all_of(other)
    assert experimental_rows(env, mine.user_id) == 1
    assert experimental_rows(env, other.user_id) == 0  # never computed, never stored
    c1x = next(r for r in await env.found(mine) if r.rule_id == "C1x")
    assert (c1x.tier, c1x.reference_day, c1x.status, c1x.origin) == (
        "experimental", day_of(60), "ended", "backfill",
    )  # fmt: skip
    assert my_connection != other_connection


async def test_a_switch_turned_off_later_removes_the_stored_experimental_rows(env: Env) -> None:
    ctx, _ = await a_year(env)
    await env.all_of(ctx, experimental=True)
    assert experimental_rows(env, ctx.user_id) == 1
    outcome = await env.evaluate(ctx)  # the switch is off now
    assert outcome.deleted == 1
    assert experimental_rows(env, ctx.user_id) == 0
    assert {r.rule_id for r in await env.found(ctx)} == {"C1"}  # the trend rows stay


async def test_the_stored_switch_off_wins_over_the_environment(env: Env) -> None:
    ctx, _ = await a_year(env)
    env.jenv.sql(
        "INSERT INTO app_settings (flag, value) VALUES ('experimental_findings', '\"off\"') "
        "ON CONFLICT (flag) DO UPDATE SET value = EXCLUDED.value"
    )
    try:
        await env.all_of(ctx, experimental=True)
    finally:
        env.jenv.sql("DELETE FROM app_settings WHERE flag = 'experimental_findings'")
    assert experimental_rows(env, ctx.user_id) == 0


# --- Q1 (system) ---------------------------------------------------------------------------------


async def test_q1_opens_updates_and_ends_from_todays_state(env: Env) -> None:
    ctx, connection = await a_year(env)
    await env.all_of(ctx)
    env.connection_status(connection, "needs_reconnect")
    env.jenv.sql(
        "INSERT INTO devices (owner_id, connection_id, hash_deviceid, battery) VALUES "
        "(:o, :c, 'synthetic_device_a', 'low'), (:o, :c, 'synthetic_device_b', 'medium')",
        o=ctx.user_id,
        c=connection,
    )
    opened = await env.evaluate(ctx)
    assert (opened.inserted, opened.pending) == (2, 2)
    q1 = [r for r in await env.found(ctx) if r.rule_id.startswith("Q1")]
    assert [(r.rule_id, r.subject, r.tier, r.severity, r.confidence, r.status) for r in q1] == [
        ("Q1:low_battery", "synthetic_device_a", "system", "info", "high", "active"),
        ("Q1:reconnect", "connection", "system", "urgent", "high", "active"),
    ]
    # The next day the same state updates the episodes; nothing new notifies.
    later = await env.evaluate(ctx, now=NOW + timedelta(days=1))
    assert (later.inserted, later.updated, later.pending) == (0, 2, 0)
    q1_later = [r for r in await env.found(ctx) if r.rule_id.startswith("Q1")]
    assert [r.id for r in q1_later] == [r.id for r in q1]
    assert {r.window_end for r in q1_later} == {TODAY + timedelta(days=1)}
    assert {r.reference_day for r in q1_later} == {TODAY}
    # Reconnected and the device gone: both end, and stay as history.
    env.connection_status(connection, "connected")
    env.jenv.sql("DELETE FROM devices WHERE owner_id = :o", o=ctx.user_id)
    ended = await env.evaluate(ctx, now=NOW + timedelta(days=2))
    assert ended.ended == 2
    final = [r for r in await env.found(ctx) if r.rule_id.startswith("Q1")]
    assert {(r.status, r.ended_at) for r in final} == {("ended", datetime(2026, 11, 3, tzinfo=UTC))}
    assert (await env.evaluate(ctx, now=NOW + timedelta(days=2))).status == "current"


async def test_q1_no_weigh_in_and_import_incomplete(env: Env) -> None:
    ctx, connection = await a_year(env)
    env.jenv.sql(
        "INSERT INTO sync_cursors (owner_id, connection_id, stream, capability, backfill_state) "
        "VALUES (:o, :c, 'measures', 'supported', '{\"status\": \"complete\"}'), "
        "(:o, :c, 'sleep_summary', 'supported', '{\"status\": \"running\"}')",
        o=ctx.user_id,
        c=connection,
    )
    await env.all_of(ctx, now=NOW + timedelta(days=14))  # the last weigh-in was 15 days ago
    q1 = {r.rule_id: r for r in await env.found(ctx) if r.rule_id.startswith("Q1")}
    assert set(q1) == {"Q1:no_weigh_in", "Q1:import_incomplete"}
    assert q1["Q1:no_weigh_in"].reference_day == day_of(364)
    assert q1["Q1:no_weigh_in"].metric_keys == ["weight"]
    assert q1["Q1:import_incomplete"].subject == "sleep_summary"
    # An import in progress is not incomplete.
    env.jenv.job(
        ctx.user_id,
        kind="withings_backfill",
        input=json.dumps({"connection_id": str(connection), "stream": "sleep_summary"}),
    )
    await env.evaluate(ctx, now=NOW + timedelta(days=14))
    incomplete = [r for r in await env.found(ctx) if r.rule_id == "Q1:import_incomplete"]
    assert [r.status for r in incomplete] == ["ended"]


# --- stale inputs write nothing (the purge race of #225) ---------------------------------------


async def test_a_purge_between_the_read_and_the_write_writes_nothing(env: Env) -> None:
    ctx, _ = await a_year(env)
    await env.derive(ctx)

    async def purge() -> None:
        await purge_imported(env.factory, ctx)

    assert (await env.evaluate(ctx, between=purge)).status == "stale"
    assert await env.found(ctx) == []
    assert (await env.evaluate(ctx)).status == "nothing"


async def test_a_new_ingest_between_the_read_and_the_write_writes_nothing(env: Env) -> None:
    ctx, connection = await a_year(env)
    await env.derive(ctx)

    async def ingest_more() -> None:
        await env.ingest(ctx, connection, *year_groups(range(YEAR, YEAR + 1)))

    assert (await env.evaluate(ctx, between=ingest_more)).status == "stale"
    assert await env.found(ctx) == []
    assert (await env.evaluate(ctx)).status == "behind"  # the normaliser has not run yet


async def test_changed_daily_values_alone_are_stale(env: Env) -> None:
    """The inputs are gathered again inside the write transaction, revision unchanged."""
    ctx, _ = await a_year(env)
    await env.derive(ctx)

    async def edit() -> None:
        env.jenv.sql(
            "UPDATE metric_daily SET value = value + 1, morning = morning + 1, min = min + 1, "
            "max = max + 1, mean = mean + 1 WHERE owner_id = :o AND metric_key = 'heart_pulse' "
            "AND day = :d",
            o=ctx.user_id,
            d=day_of(10),
        )

    assert (await env.evaluate(ctx, between=edit)).status == "stale"
    assert await env.found(ctx) == []


async def test_a_switch_change_between_the_read_and_the_write_is_stale(env: Env) -> None:
    ctx, _ = await a_year(env)
    await env.derive(ctx)

    async def switch_off() -> None:
        env.jenv.sql(
            "INSERT INTO app_settings (flag, value) VALUES ('experimental_findings', '\"off\"')"
        )

    try:
        assert (await env.evaluate(ctx, experimental=True, between=switch_off)).status == "stale"
    finally:
        env.jenv.sql("DELETE FROM app_settings WHERE flag = 'experimental_findings'")
    assert await env.found(ctx) == []


async def test_behind_until_the_daily_pass_has_caught_up(env: Env) -> None:
    ctx, connection = await a_year(env)
    await env.all_of(ctx)
    await env.ingest(ctx, connection, *year_groups(range(YEAR, YEAR + 1)))
    assert (await env.evaluate(ctx)).status == "behind"  # observations behind the source
    await normalise_owner(env.opener(ctx), ctx.user_id, now=NOW)
    assert (await env.evaluate(ctx)).status == "behind"  # daily values behind the observations
    await daily_owner(env.opener(ctx), ctx.user_id)
    assert (await env.evaluate(ctx)).status == "written"


async def test_nothing_imported_is_nothing(env: Env) -> None:
    ctx, _ = env.owner()
    assert (await env.evaluate(ctx)).status == "nothing"


async def test_the_write_holds_the_revision_row_so_a_purge_waits_for_it(env: Env) -> None:
    ctx, _ = await a_year(env)
    await env.derive(ctx)
    state = {"writing": False}
    blocked: list[bool] = []
    purge_task: list[asyncio.Task[None]] = []

    async def mark() -> None:
        state["writing"] = True

    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with user_session(env.factory, ctx) as db:
            yield db
            if state["writing"]:
                purge_task.append(asyncio.ensure_future(purge_imported(env.factory, ctx)))
                await asyncio.sleep(0.5)
                blocked.append(not purge_task[0].done())

    outcome = await evaluate_owner(
        open_tx, ctx.user_id, settings=env.settings(), now=NOW, between=mark
    )
    await purge_task[0]
    assert outcome.status == "written"
    assert blocked == [True]
    assert await env.found(ctx) == []  # the purge ran after the commit and removed them


async def test_the_write_takes_the_owners_normalisation_lock(env: Env) -> None:
    ctx, _ = await a_year(env)
    await env.derive(ctx)
    state = {"writing": False}
    held: list[int] = []

    async def mark() -> None:
        state["writing"] = True

    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with user_session(env.factory, ctx) as db:
            yield db
            if state["writing"]:
                row = await db.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                        "AND pid = pg_backend_pid() AND classid = :ns "
                        "AND objid = pg_catalog.hashtext(CAST(:o AS text))::oid"
                    ),
                    {"ns": NORMALISATION, "o": ctx.user_id},
                )
                held.append(int(row.scalar_one()))

    outcome = await evaluate_owner(
        open_tx, ctx.user_id, settings=env.settings(), now=NOW, between=mark
    )
    assert outcome.status == "written"
    assert held == [1]


async def test_two_concurrent_passes_for_one_owner_do_not_collide(env: Env) -> None:
    ctx, _ = await a_year(env)
    await env.derive(ctx)
    arrived = 0
    both_read = asyncio.Event()

    async def rendezvous() -> None:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_read.set()
        await asyncio.wait_for(both_read.wait(), 30)

    outcomes = await asyncio.gather(
        env.evaluate(ctx, between=rendezvous), env.evaluate(ctx, between=rendezvous)
    )
    assert sorted(o.status for o in outcomes) == ["current", "written"]
    assert sorted(o.inserted for o in outcomes) == [0, 2]
    assert len(await env.found(ctx)) == 2


# --- scope: this writer's rules, this owner -------------------------------------------------------


async def test_another_writers_rules_are_never_touched(env: Env) -> None:
    """A rule id outside ``OWNED_RULES`` (E5, deferred by ADR 0006 section 12; E1 to E4 and E6
    are this writer's since #191) is never read, changed or deleted."""
    ctx, _ = await a_year(env)
    env.jenv.sql(
        "INSERT INTO findings (owner_id, rule_id, rule_version, tier, severity, confidence, "
        "origin, subject, window_start, window_end, evidence, input_revision, input_fingerprint) "
        "VALUES (:o, 'E5', 'synthetic/1', 'experimental', 'attention', 'medium', 'live', "
        "'ecg', '2026-10-01', '2026-10-01', '{}', 99, decode(repeat('ab', 32), 'hex'))",
        o=ctx.user_id,
    )
    await env.all_of(ctx, experimental=True)
    e5 = [r for r in await env.found(ctx) if r.rule_id == "E5"]
    assert [(r.rule_version, r.input_revision, bytes(r.input_fingerprint)) for r in e5] == [
        ("synthetic/1", 99, bytes.fromhex("ab" * 32))
    ]


async def test_only_the_owners_rows_are_read_and_written(env: Env) -> None:
    mine, my_connection = await a_year(env)
    other, other_connection = env.owner()
    await env.ingest(other, other_connection, *year_groups(range(100)))
    await env.all_of(mine)
    assert await env.found(other) == []
    await env.all_of(other)
    assert len(await env.found(mine)) == 2
    assert await env.found(other) == []  # no C1 run in the other owner's first 100 days
    assert my_connection != other_connection


# --- the job ------------------------------------------------------------------------------------


async def test_the_job_runs_in_the_workers_context_and_sees_only_its_owner(env: Env) -> None:
    """RLS in the worker-job context: the pass runs as the owner under the fence."""
    jenv = env.jenv
    mine, my_connection = await a_year(env)
    other, other_connection = env.owner()
    await env.ingest(other, other_connection, *year_groups())
    await env.derive(mine)
    await env.derive(other)
    registry = KindRegistry()
    register_evaluate_kind(registry)
    register_evaluate_kind(registry)  # twice is one registration
    assert registry.names() == [KIND]
    payload = json.dumps({"connection_id": str(my_connection), "origin": "live"})
    job_id = jenv.job(mine.user_id, kind=KIND, input=payload)[1]
    claimed = await queue.claim(env.factory, "w", 60, [KIND])
    assert claimed is not None
    assert claimed.owner_id == mine.user_id
    execution = Execution(claimed)
    config = RunConfig(lease_seconds=60, accounts=jenv.accounts)
    await execute(execution, env.factory, registry, config, asyncio.Event())
    assert (execution.result, jenv.row(job_id).state) == ("completed", "completed")
    assert len(await env.found(mine)) == 2
    assert await env.found(other) == []
    assert other_connection != my_connection


async def test_enqueue_keeps_live_and_backfill_apart_and_collapses_repeats(env: Env) -> None:
    ctx, connection = env.owner()
    for origin in ("live", "backfill", "live", "backfill", "live"):
        await enqueue_evaluate(env.factory, connection, origin)  # pyright: ignore[reportArgumentType]
    rows = env.jenv.sql(
        "SELECT input, singleton_key, state FROM jobs WHERE owner_id = :o AND kind = :k "
        "ORDER BY singleton_key",
        o=ctx.user_id,
        k=KIND,
    ).all()
    assert [(r.singleton_key, r.state) for r in rows] == [
        ("evaluate_backfill", "queued"),
        ("evaluate_live", "queued"),
    ]
    assert [EvaluateInput.model_validate(r.input).origin for r in rows] == ["backfill", "live"]


def test_the_kind_is_registered_in_the_worker_only() -> None:
    assert KIND in worker_registry().names()
    assert KINDS.get(KIND) is None


async def run_daily_job(env: Env, ctx: UserContext, connection: uuid.UUID, origin: str) -> str:
    registry = KindRegistry()
    register_daily_kind(registry)
    payload = json.dumps({"connection_id": str(connection), "origin": origin})
    env.jenv.job(ctx.user_id, kind=DAILY_KIND, input=payload)
    claimed = await queue.claim(env.factory, "w", 60, [DAILY_KIND])
    assert claimed is not None
    execution = Execution(claimed)
    config = RunConfig(lease_seconds=60, accounts=env.jenv.accounts)
    await execute(execution, env.factory, registry, config, asyncio.Event())
    return str(execution.result)


def queued_evaluations(env: Env, ctx: UserContext) -> list[tuple[str, str]]:
    rows = env.jenv.sql(
        "SELECT singleton_key, input->>'origin' AS origin FROM jobs WHERE owner_id = :o "
        "AND kind = :k ORDER BY singleton_key",
        o=ctx.user_id,
        k=KIND,
    ).all()
    return [(r.singleton_key, r.origin) for r in rows]


async def test_the_daily_pass_queues_the_evaluation_with_its_own_origin(env: Env) -> None:
    ctx, connection = env.owner()
    await env.ingest(ctx, connection, *year_groups(range(3)))
    await normalise_owner(env.opener(ctx), ctx.user_id, now=NOW)
    assert await run_daily_job(env, ctx, connection, "backfill") == "completed"
    assert queued_evaluations(env, ctx) == [("evaluate_backfill", "backfill")]
    assert await run_daily_job(env, ctx, connection, "live") == "completed"  # `current`
    assert queued_evaluations(env, ctx) == [
        ("evaluate_backfill", "backfill"),
        ("evaluate_live", "live"),
    ]


async def test_a_daily_pass_with_nothing_imported_queues_no_evaluation(env: Env) -> None:
    ctx, connection = env.owner()
    assert await run_daily_job(env, ctx, connection, "live") == "completed"  # `nothing`
    assert queued_evaluations(env, ctx) == []
