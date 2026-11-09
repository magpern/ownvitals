"""The ``estimated`` recompute and its sweep against the database (#191; ADR 0006 sections 1, 9 and
15): a stale or missing ``estimated`` is recomputed, a current one is left alone, the write bumps
no revision and queues no evaluation, a rewrite or a purge between read and write wins, an
``hrv``-only difference keeps the gate result until the sweep, and the job runs as its owner in the
worker's context.

**Synthetic** strips only (``ecgsynth.make_strip``: generated waveforms, made-up signal ids); no
real recording.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text

from ecgsynth import make_strip
from jobcore import JobsEnv, jenv_fixture  # noqa: F401 - fixtures
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.connections.purge import purge_imported
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.derived.ecg_recompute import (
    KIND,
    RecomputeInput,
    compute,
    enqueue_recompute,
    is_current,
    recompute_owner,
    register_recompute_kind,
)
from ownvitals.derived.evaluate_job import KIND as EVALUATE_KIND
from ownvitals.ecg.overlay import gate_result
from ownvitals.ecg.pipeline import MODULES, PIPELINE_VERSION
from ownvitals.ingest.codec import input_fingerprint
from ownvitals.ingest.rows import upsert_ecg
from ownvitals.jobs import KINDS, KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.withings.models import HeartListEntry, HeartSignal
from ownvitals.withings.sync_kinds import after_ecg
from ownvitals.worker.kinds import worker_registry
from test_derived_normalize_db import Env as NormalizeEnv

pytestmark = pytest.mark.usefixtures("jenv")

SYNTHETIC = True
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
WHEN = int(datetime(2026, 10, 30, 7, tzinfo=UTC).timestamp())
ROWS = (
    "SELECT signalid, payload_hash, sampling_hz, estimated FROM ecg_recordings "
    "WHERE owner_id = :o ORDER BY signalid"
)


def entry(signalid: int, *, at: int = WHEN, afib: int = 0) -> HeartListEntry:
    return HeartListEntry.model_validate(
        {
            "timestamp": at,
            "ecg": {"signalid": signalid, "afib": afib},
            "deviceid": "synthetic_device",
            "timezone": "UTC",
        }
    )


def signal(seed: int = 3, hr: float = 70.0) -> HeartSignal:
    strip = make_strip(seed, hr, 300)
    return HeartSignal.model_validate(
        {"signal": [int(v) for v in strip.signal_uv], "sampling_frequency": 300}
    )


class Env(NormalizeEnv):
    async def ecg(
        self, ctx: UserContext, connection: uuid.UUID, signalid: int, sig: HeartSignal | None
    ) -> str:
        async with user_session(self.factory, ctx) as db:
            return await upsert_ecg(db, ctx.user_id, connection, entry(signalid), sig, "UTC", NOW)

    async def recompute(self, ctx: UserContext, **kw: Any) -> Any:
        return await recompute_owner(self.opener(ctx), ctx.user_id, **kw)

    async def stored(self, ctx: UserContext) -> list[Any]:
        async with user_session(self.factory, ctx) as db:
            return list((await db.execute(text(ROWS), {"o": ctx.user_id})).all())

    def revision(self, ctx: UserContext) -> int | None:
        return self.jenv.sql(
            "SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = 'ecg'",
            o=ctx.user_id,
        ).scalar_one_or_none()

    def set_estimated(self, ctx: UserContext, signalid: int, estimated: dict[str, Any]) -> None:
        self.jenv.sql(
            "UPDATE ecg_recordings SET estimated = CAST(:e AS jsonb) "
            "WHERE owner_id = :o AND signalid = :s",
            e=json.dumps(estimated),
            o=ctx.user_id,
            s=signalid,
        )


@pytest.fixture
def env(jenv: JobsEnv) -> Env:
    return Env(jenv)


def test_is_current_needs_the_running_version_and_the_rows_fingerprint() -> None:
    digest = b"\x01" * 32
    fp = input_fingerprint(9, digest, 300)
    assert is_current(PIPELINE_VERSION, fp, signalid=9, digest=digest, sampling_hz=300)
    assert not is_current("ecg-0.0.1", fp, signalid=9, digest=digest, sampling_hz=300)
    assert not is_current(PIPELINE_VERSION, fp, signalid=9, digest=digest, sampling_hz=500)
    assert not is_current(None, None, signalid=9, digest=digest, sampling_hz=300)


def test_an_undecodable_frame_gets_a_signal_missing_estimate() -> None:
    record, broken = compute(9, b"not a zstd frame", b"\x01" * 32, 300, 4)
    assert broken
    assert (record["sqi"]["result"], record["sqi"]["reasons"]) == ("not_run", ["signal_missing"])
    assert record["input_fingerprint"] == input_fingerprint(9, b"\x01" * 32, 300)


async def test_a_new_row_is_recomputed_once_and_a_second_run_writes_nothing(env: Env) -> None:
    ctx, connection = env.owner()
    assert await env.ecg(ctx, connection, 11, signal()) == "inserted"
    assert await env.ecg(ctx, connection, 12, None) == "inserted"  # signal_missing at ingest
    revision = env.revision(ctx)
    (row, missing) = await env.stored(ctx)
    assert row.estimated is None
    first = await env.recompute(ctx)
    assert (first.current, first.recomputed, first.moved) == (1, 1, 0)
    (row, again) = await env.stored(ctx)
    assert row.estimated["algo_version"] == PIPELINE_VERSION
    assert row.estimated["input_fingerprint"] == input_fingerprint(11, bytes(row.payload_hash), 300)
    assert row.estimated["sqi"]["result"] in ("pass", "fail")
    assert again.estimated == missing.estimated
    assert env.revision(ctx) == revision  # writing estimated bumps nothing (section 15)
    second = await env.recompute(ctx)
    assert (second.current, second.recomputed, second.moved) == (2, 0, 0)


async def test_the_recompute_queues_no_evaluation(env: Env) -> None:
    ctx, connection = env.owner()
    await env.ecg(ctx, connection, 11, signal())
    await env.recompute(ctx)
    queued = env.jenv.sql(
        "SELECT count(*) FROM jobs WHERE owner_id = :o AND kind = :k",
        o=ctx.user_id,
        k=EVALUATE_KIND,
    ).scalar_one()
    assert queued == 0


async def test_the_sweep_recomputes_an_old_version_and_an_hrv_only_one_keeps_its_gate(
    env: Env,
) -> None:
    """A module version bump: the stored estimate is of another pipeline version. With only
    ``hrv`` changed the gate result stays current until the sweep; with a gate module changed it
    is absent at once (ADR 0006 section 15)."""
    ctx, connection = env.owner()
    await env.ecg(ctx, connection, 11, signal())
    await env.ecg(ctx, connection, 13, signal(seed=4))
    await env.recompute(ctx)
    (row, other) = await env.stored(ctx)
    fp = input_fingerprint(11, bytes(row.payload_hash), 300)
    gate = gate_result(row.estimated, fp)
    assert gate in ("pass", "fail")
    hrv_only = {
        **row.estimated,
        "algo_version": "ecg-4.9.0",
        "modules": {**MODULES, "hrv": "0.0.1"},
    }
    env.set_estimated(ctx, 11, hrv_only)
    other_fp = input_fingerprint(13, bytes(other.payload_hash), 300)
    gate_changed = {**other.estimated, "algo_version": "ecg-4.9.0"}
    gate_changed["modules"] = {**MODULES, "sqi": "0.0.1"}
    env.set_estimated(ctx, 13, gate_changed)
    assert gate_result(hrv_only, fp) == gate  # the gate result is kept
    assert gate_result(gate_changed, other_fp) is None  # absent until recomputed
    swept = await env.recompute(ctx)
    assert (swept.current, swept.recomputed) == (0, 2)
    assert {r.estimated["algo_version"] for r in await env.stored(ctx)} == {PIPELINE_VERSION}


async def test_a_rewritten_signal_between_read_and_write_is_not_overwritten(env: Env) -> None:
    ctx, connection = env.owner()
    await env.ecg(ctx, connection, 11, signal(seed=3))

    async def rewrite() -> None:
        assert await env.ecg(ctx, connection, 11, signal(seed=8, hr=90.0)) == "updated"

    outcome = await env.recompute(ctx, between=rewrite)
    assert (outcome.recomputed, outcome.moved) == (0, 1)
    (row,) = await env.stored(ctx)
    assert row.estimated is None  # left to the recompute the rewrite queues
    assert (await env.recompute(ctx)).recomputed == 1


async def test_a_purge_between_read_and_write_leaves_nothing_to_write(env: Env) -> None:
    ctx, connection = env.owner()
    await env.ecg(ctx, connection, 11, signal())

    async def purge() -> None:
        await purge_imported(env.factory, ctx)

    outcome = await env.recompute(ctx, between=purge)
    assert (outcome.recomputed, outcome.moved) == (0, 1)
    assert await env.stored(ctx) == []


async def test_the_job_runs_in_the_workers_context_and_touches_only_its_owner(env: Env) -> None:
    """RLS in the worker-job context: the recompute runs as the owner under the fence."""
    jenv = env.jenv
    mine, my_connection = env.owner()
    other, other_connection = env.owner()
    await env.ecg(mine, my_connection, 11, signal())
    await env.ecg(other, other_connection, 11, signal())
    registry = KindRegistry()
    register_recompute_kind(registry)
    register_recompute_kind(registry)  # twice is one registration
    assert registry.names() == [KIND]
    payload = json.dumps({"connection_id": str(my_connection), "origin": "backfill"})
    job_id = jenv.job(mine.user_id, kind=KIND, input=payload)[1]
    claimed = await queue.claim(env.factory, "w", 60, [KIND])
    assert claimed is not None
    assert claimed.owner_id == mine.user_id
    execution = Execution(claimed)
    config = RunConfig(lease_seconds=60, accounts=jenv.accounts)
    await execute(execution, env.factory, registry, config, asyncio.Event())
    assert (execution.result, jenv.row(job_id).state) == ("completed", "completed")
    assert [r.estimated is not None for r in await env.stored(mine)] == [True]
    assert [r.estimated is None for r in await env.stored(other)] == [True]


async def test_enqueue_collapses_and_an_ecg_pass_queues_the_recompute_and_an_evaluation(
    env: Env,
) -> None:
    ctx, connection = env.owner()
    for _ in range(3):
        await enqueue_recompute(env.factory, connection)
    await after_ecg(SimpleNamespace(factory=env.factory), connection, "live")  # pyright: ignore[reportArgumentType]
    rows = env.jenv.sql(
        "SELECT kind, singleton_key, input FROM jobs WHERE owner_id = :o AND state = 'queued' "
        "ORDER BY kind",
        o=ctx.user_id,
    ).all()
    assert [(r.kind, r.singleton_key) for r in rows] == [
        ("derive_evaluate", "evaluate_live"),
        (KIND, "ecg_recompute"),
    ]
    assert RecomputeInput.model_validate(rows[1].input).origin == "backfill"


def test_the_kind_is_registered_in_the_worker_only() -> None:
    assert KIND in worker_registry().names()
    assert KINDS.get(KIND) is None
