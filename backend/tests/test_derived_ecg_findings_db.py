"""E1 to E4 and E6 through the evaluation pass against the database (#191; ADR 0006 section 11):
recordings and ECG measure groups through the ingest code, the normaliser and the daily pass, the
evaluation as ``ownvitals_app``. An E1 found in history is ``backfill`` and never notifies; the
A7-linked groups are E1's triggers; E4 reads the profile's sex behind the switch; writing
``estimated`` re-evaluates nothing; a new recording between read and write is ``stale``; the job
sees only its owner's recordings in the worker's context.

**Synthetic** users, groups, recordings and values only (made-up ids and codes).
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest

from jobcore import JobsEnv, jenv_fixture  # noqa: F401 - fixtures
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.derived.ecg_recompute import recompute_owner
from ownvitals.derived.evaluate_job import KIND, register_evaluate_kind
from ownvitals.ingest.rows import upsert_ecg
from ownvitals.jobs import KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.withings.models import HeartListEntry
from test_derived_evaluate_db import NOW
from test_derived_evaluate_db import Env as EvaluateEnv
from test_derived_normalize_db import group

pytestmark = pytest.mark.usefixtures("jenv")

SYNTHETIC = True
DEVICE = "synthetic_device"
RECENT = int(datetime(2026, 10, 31, 7, tzinfo=UTC).timestamp())  # yesterday: live
OLD = int(datetime(2026, 6, 1, 7, tzinfo=UTC).timestamp())  # five months back: history
DAY = 86_400


def recording(signalid: int, at: int, **ecg: int) -> HeartListEntry:
    return HeartListEntry.model_validate(
        {
            "timestamp": at,
            "ecg": {"signalid": signalid, **ecg},
            "deviceid": DEVICE,
            "timezone": "UTC",
        }
    )


class Env(EvaluateEnv):
    async def record(
        self, ctx: UserContext, connection: uuid.UUID, *entries: HeartListEntry
    ) -> None:
        async with user_session(self.factory, ctx) as db:
            for e in entries:
                await upsert_ecg(db, ctx.user_id, connection, e, None, "UTC", NOW)

    def profile(self, ctx: UserContext, sex: str) -> None:
        self.jenv.sql(
            "INSERT INTO user_profiles (owner_id, sex) VALUES (:o, :s)", o=ctx.user_id, s=sex
        )


@pytest.fixture
def env(jenv: JobsEnv) -> Env:
    return Env(jenv)


async def setup(env: Env) -> tuple[UserContext, uuid.UUID]:
    """A weigh-in (the ``measures`` revision), an old AFib-positive recording, a negative one the
    day after, and a recent positive one whose ECG measure group (130 and QTc 138) A7 links."""
    ctx, connection = env.owner()
    await env.ingest(
        ctx,
        connection,
        group(1, RECENT - DAY, (1, 80_000, -3, None)),
        group(2, RECENT, (130, 1, 0, None), (138, 465, 0, None), hash_deviceid=DEVICE),
        group(3, RECENT, (130, 1, 0, None), hash_deviceid="synthetic_other_device"),  # no A7 link
    )
    await env.record(
        ctx,
        connection,
        recording(21, OLD, afib=1),
        recording(22, OLD + DAY, afib=0),
        recording(23, RECENT, afib=0, afib_details=11),
    )
    await env.derive(ctx)
    return ctx, connection


def e_rows(rows: list) -> list:
    return [r for r in rows if r.rule_id.startswith("E")]


async def test_an_e1_found_in_history_goes_to_history_without_a_notification(env: Env) -> None:
    ctx, _ = await setup(env)
    assert (await env.evaluate(ctx)).status == "written"
    found = e_rows(await env.found(ctx))
    e1 = [(r.reference_day.isoformat(), r.status, r.origin, r.notify_state) for r in found]
    assert e1 == [
        ("2026-06-01", "ended", "backfill", "none"),
        ("2026-10-31", "active", "live", "pending"),
    ]
    assert {(r.rule_id, r.tier, r.severity, r.subject) for r in found} == {
        ("E1", "device", "urgent", "ecg")
    }
    assert found[1].evidence["trigger_groups"] == [2]  # A7: same moment and device only
    assert found[1].evidence["recordings"] == [{"signalid": 23, "afib": 0, "afib_details": 11}]


async def test_e4_reads_the_profiles_sex_behind_the_switch(env: Env) -> None:
    ctx, _ = await setup(env)
    await env.evaluate(ctx)
    assert "E4" not in {r.rule_id for r in await env.found(ctx)}
    await env.evaluate(ctx, experimental=True)
    assert "E4" not in {r.rule_id for r in await env.found(ctx)}  # 465 ms, sex unknown
    env.profile(ctx, "male")
    await env.evaluate(ctx, experimental=True)
    (e4,) = [r for r in await env.found(ctx) if r.rule_id == "E4"]
    assert (e4.tier, e4.subject, e4.evidence["band"]) == (
        "experimental",
        "qtc_interval",
        "qtc_male",
    )
    assert e4.evidence["trigger_groups"] == [2]


async def test_writing_estimated_re_evaluates_nothing(env: Env) -> None:
    ctx, _ = await setup(env)
    await env.evaluate(ctx)
    before = await env.found(ctx)
    await recompute_owner(env.opener(ctx), ctx.user_id)
    assert (await env.evaluate(ctx)).status == "current"
    assert await env.found(ctx) == before


async def test_a_new_recording_between_the_read_and_the_write_is_stale(env: Env) -> None:
    ctx, connection = await setup(env)

    async def record() -> None:
        await env.record(ctx, connection, recording(24, RECENT + 3600, afib=1))

    assert (await env.evaluate(ctx, between=record)).status == "stale"
    assert e_rows(await env.found(ctx)) == []
    assert (await env.evaluate(ctx)).status == "written"


async def test_a_changed_code_re_derives_e1(env: Env) -> None:
    ctx, connection = await setup(env)
    await env.evaluate(ctx)
    await env.record(ctx, connection, recording(21, OLD, afib=0))  # Withings revised it
    await env.evaluate(ctx)
    found = e_rows(await env.found(ctx))
    assert [r.reference_day.isoformat() for r in found] == ["2026-10-31"]


async def test_the_job_sees_only_its_owners_recordings_in_the_workers_context(env: Env) -> None:
    """RLS in the worker-job context: the other owner's positive recording gives this owner
    nothing, and the job writes nothing for the other owner."""
    jenv = env.jenv
    mine, my_connection = env.owner()
    await env.ingest(mine, my_connection, group(1, RECENT - DAY, (1, 80_000, -3, None)))
    await env.record(mine, my_connection, recording(31, RECENT, afib=0))
    other, other_connection = await setup(env)
    await env.derive(mine)
    registry = KindRegistry()
    register_evaluate_kind(registry)
    payload = json.dumps({"connection_id": str(my_connection), "origin": "live"})
    job_id = jenv.job(mine.user_id, kind=KIND, input=payload)[1]
    claimed = await queue.claim(env.factory, "w", 60, [KIND])
    assert claimed is not None
    assert claimed.owner_id == mine.user_id
    execution = Execution(claimed)
    config = RunConfig(lease_seconds=60, accounts=jenv.accounts)
    await execute(execution, env.factory, registry, config, asyncio.Event())
    assert (execution.result, jenv.row(job_id).state) == ("completed", "completed")
    assert e_rows(await env.found(mine)) == []
    assert await env.found(other) == []
    assert other_connection != my_connection
