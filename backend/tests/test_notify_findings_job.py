"""The ``notify_findings`` job (#195): a pass after a real rule evaluation, the job in the
worker's context (fenced, as the owner under RLS), and its wiring after the evaluation and the
``estimated`` recompute. Synthetic accounts, groups and recordings; fakes only, no network.
"""

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from jobcore import JobsEnv, jenv_fixture  # noqa: F401 - fixtures
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.config_notify import NotifySettings
from ownvitals.context import UserContext
from ownvitals.derived.ecg_recompute import KIND as RECOMPUTE_KIND
from ownvitals.derived.evaluate_job import KIND as EVALUATE_KIND
from ownvitals.jobs import KINDS, Complete, JobKind, JobRun, KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.notify.findings import notify_owner
from ownvitals.notify.findings_job import KIND, register_notify_kind, then_notify
from ownvitals.worker.kinds import worker_registry
from test_derived_ecg_findings_db import Env as EcgEnv
from test_derived_ecg_findings_db import e_rows, setup
from test_derived_evaluate_db import NOW

pytestmark = pytest.mark.usefixtures("jenv")

SYNTHETIC = True


class Env(EcgEnv):
    def email_on(self, ctx: UserContext) -> None:
        self.jenv.sql(
            "INSERT INTO user_profiles (owner_id, notify_email) VALUES (:o, true) "
            "ON CONFLICT (owner_id) DO UPDATE SET notify_email = true",
            o=ctx.user_id,
        )

    def mails(self, ctx: UserContext) -> list[str]:
        rows = self.jenv.sql(
            "SELECT kind FROM mail_outbox WHERE owner_id = :o", o=ctx.user_id
        ).all()
        return [str(r.kind) for r in rows]

    def jobs_of(self, ctx: UserContext, kind: str) -> list[Any]:
        return list(
            self.jenv.sql(
                "SELECT id, state, input FROM jobs WHERE owner_id = :o AND kind = :k",
                o=ctx.user_id,
                k=kind,
            ).all()
        )

    async def run_job(self, registry: KindRegistry, kind: str) -> Execution:
        claimed = await queue.claim(self.factory, "w", 60, [kind])
        assert claimed is not None
        execution = Execution(claimed)
        config = RunConfig(lease_seconds=60, accounts=self.jenv.accounts)
        await execute(execution, self.factory, registry, config, asyncio.Event())
        return execution


@pytest.fixture
def env(jenv: JobsEnv) -> Env:
    return Env(jenv)


async def test_after_a_rule_evaluation_only_the_live_episode_notifies(env: Env) -> None:
    """The rules engine writes a live E1 (pending) and a backfill one (none); the pass sends the
    live one, once, and a re-evaluation that updates it notifies nothing more."""
    ctx, _ = await setup(env)
    env.email_on(ctx)
    await env.evaluate(ctx)
    outcome = await notify_owner(
        env.opener(ctx), ctx.user_id, settings=env.settings(), bound=timedelta(hours=24), now=NOW
    )
    assert outcome.sent == 1
    found = e_rows(await env.found(ctx))
    assert [(r.origin, r.notify_state) for r in found] == [("backfill", "none"), ("live", "sent")]
    assert env.mails(ctx) == ["findings_waiting"]
    env.jenv.sql(
        "UPDATE mail_outbox SET state = 'sent', sent_at = now() WHERE owner_id = :o", o=ctx.user_id
    )
    await env.evaluate(ctx, now=NOW + timedelta(days=1))  # an update of the same episode
    again = await notify_owner(
        env.opener(ctx),
        ctx.user_id,
        settings=env.settings(),
        bound=timedelta(hours=24),
        now=NOW + timedelta(days=1),
    )
    assert again.sent == 0
    assert env.mails(ctx) == ["findings_waiting"]


async def test_the_job_notifies_its_owner_only_in_the_workers_context(env: Env) -> None:
    mine, connection = await setup(env)
    other, other_connection = await setup(env)
    await env.evaluate(mine)
    await env.evaluate(other)
    registry = KindRegistry()
    register_notify_kind(registry)
    payload = json.dumps({"connection_id": str(connection)})
    job_id = env.jenv.job(mine.user_id, kind=KIND, input=payload)[1]
    execution = await env.run_job(registry, KIND)
    assert (execution.result, env.jenv.row(job_id).state) == ("completed", "completed")
    assert [r.notify_state for r in e_rows(await env.found(mine))] == ["none", "sent"]
    assert [r.notify_state for r in e_rows(await env.found(other))] == ["none", "pending"]
    assert other_connection != connection


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: uuid.UUID


async def test_then_notify_queues_one_pass_after_each_success(env: Env) -> None:
    ctx, connection = env.owner()
    calls: list[str] = []

    async def succeed(job: JobRun) -> Complete:
        calls.append("ran")
        return Complete()

    registry = KindRegistry()
    registry.register(then_notify(JobKind("test_kind", _Input, succeed)))
    for _ in range(2):
        env.jenv.job(
            ctx.user_id, kind="test_kind", input=json.dumps({"connection_id": str(connection)})
        )
        assert (await env.run_job(registry, "test_kind")).result == "completed"
    queued = env.jobs_of(ctx, KIND)
    assert len(queued) == 1  # one queued pass per owner (the singleton)
    assert (queued[0].state, queued[0].input) == ("queued", {"connection_id": str(connection)})
    assert calls == ["ran", "ran"]


async def test_then_notify_queues_nothing_after_a_failure(env: Env) -> None:
    from ownvitals.jobs import JobFailure

    ctx, connection = env.owner()

    async def fail(job: JobRun) -> Complete:
        raise JobFailure("internal")

    registry = KindRegistry()
    registry.register(then_notify(JobKind("test_kind", _Input, fail)))
    env.jenv.job(
        ctx.user_id,
        kind="test_kind",
        input=json.dumps({"connection_id": str(connection)}),
        max_attempts=1,
    )
    await env.run_job(registry, "test_kind")
    assert env.jobs_of(ctx, KIND) == []


def test_the_worker_registers_the_kind_and_follows_evaluate_and_recompute() -> None:
    registry = worker_registry()
    assert KIND in registry.names()
    assert KINDS.get(KIND) is None  # the API's registry stays empty
    for name in (EVALUATE_KIND, RECOMPUTE_KIND):
        kind = registry.get(name)
        assert kind is not None
        assert kind.run.__qualname__.startswith("then_notify")
    assert NotifySettings().hold_bound == timedelta(hours=24)
