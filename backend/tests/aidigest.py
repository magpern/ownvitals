"""Shared setup of the weekly digest's tests (``test_ai_digest*.py``, ``ai_evals/test_digest_*``;
issue 209). No tests here.

Builds on ``aichat`` (the app with every owner lock open, the fake model behind the real egress
client, a worker session factory): ``DigestUser`` is a signed-in user with a live connection (the
scheduler queues through it), optionally consenting and opted in; ``queue_digest`` queues the
week's job as the scheduler does; ``run_digest_job`` claims and runs one with the worker's runner;
``run_notify_pass`` runs a notification pass wrapped with ``then_digest``, as the worker's
registry does.

Synthetic accounts, dates and texts; invented prices and filler secrets; no network, no real
model output, no health data.
"""

import asyncio
import itertools
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import partial
from typing import Any

from aichat import ChatEnv, User, _no_sleep, sql, user  # pyright: ignore[reportPrivateUsage]
from ownvitals.ai.digest import run_digest, then_digest
from ownvitals.ai.digest_model import DIGEST_KIND, SINGLETON_KEY, DigestInput
from ownvitals.auth.owners import ACCOUNTS
from ownvitals.jobs import Complete, JobKind, KindRegistry, queue
from ownvitals.jobs.run import JobRun
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.notify.findings_job import KIND as NOTIFY_KIND
from ownvitals.notify.findings_job import NotifyInput
from test_ai_client_env import PRICES
from viewtools import connected_with_streams

SYNTHETIC = True
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)  # a Tuesday
WEEK = date(2026, 11, 2)  # the Monday of the last full week before NOW
DIGEST_TEXT = "Synthetic weekly summary: your synthetic weight was steady."
_WITHINGS_USERS = itertools.count(910_001)  # invented Withings user ids, one per connection


@dataclass
class DigestUser:
    user: User
    connection: uuid.UUID

    @property
    def id(self) -> uuid.UUID:
        return self.user.id


def opt_in(owner: uuid.UUID) -> None:
    sql("INSERT INTO ai_digest_prefs (owner_id) VALUES (:o) ON CONFLICT DO NOTHING", o=owner)


async def digest_user(
    chat: ChatEnv, email: str, *, consent: bool = True, opted_in: bool = True
) -> DigestUser:
    who = await user(chat, email, consent=consent)
    connection = connected_with_streams(who.id, withings_userid=next(_WITHINGS_USERS))
    if opted_in:
        opt_in(who.id)
    return DigestUser(who, connection)


async def queue_digest(chat: ChatEnv, who: DigestUser | uuid.UUID, week: date = WEEK) -> uuid.UUID:
    """The week's ``ai_digest`` job for a user (or the owner of a connection id), queued as
    ``queue_if_due`` queues it."""
    return await queue.enqueue(
        chat.worker_factory,
        connection_id=who.connection if isinstance(who, DigestUser) else who,
        kind=DIGEST_KIND,
        origin="scheduler",
        payload=json.dumps({"week_start": week.isoformat()}),
        singleton_key=SINGLETON_KEY,
        max_attempts=1,
        target_secret_id=None,
    )


def digest_options(chat: ChatEnv, **call_options: Any) -> dict[str, Any]:
    return {
        "settings": chat.env.settings,
        "now": NOW,
        "inner": chat.fake.transport,
        "prices": PRICES,
        "secrets_directory": chat.secrets,
        "sleep": _no_sleep,
        **call_options,
    }


async def _run(chat: ChatEnv, kinds: list[str], registry: KindRegistry) -> Execution | None:
    claimed = await queue.claim(chat.worker_factory, "synthetic-digest-worker", 60, kinds)
    if claimed is None:
        return None
    execution = Execution(claimed)
    config = RunConfig(lease_seconds=60, accounts=ACCOUNTS)
    await execute(execution, chat.worker_factory, registry, config, asyncio.Event())
    return execution


async def run_digest_job(chat: ChatEnv, **call_options: Any) -> Execution | None:
    """Claim and run one queued digest as the worker does; ``None`` when none is queued."""
    registry = KindRegistry()
    handler = partial(run_digest, **digest_options(chat, **call_options))
    registry.register(JobKind(DIGEST_KIND, DigestInput, handler))
    return await _run(chat, [DIGEST_KIND], registry)


async def _notified(job: JobRun) -> Complete:
    del job
    return Complete()


async def run_notify_pass(chat: ChatEnv, who: DigestUser, **settings: Any) -> Execution | None:
    """Queue and run one notification pass for ``who`` (the pass itself does nothing here),
    wrapped with ``then_digest`` and the test's settings and prices."""
    await queue.enqueue(
        chat.worker_factory,
        connection_id=who.connection,
        kind=NOTIFY_KIND,
        origin="scheduler",
        payload=json.dumps({"connection_id": str(who.connection)}),
        singleton_key="notify_findings",
        max_attempts=3,
        target_secret_id=None,
    )
    current = chat.env.settings.model_copy(update=settings)
    registry = KindRegistry()
    registry.register(
        then_digest(
            JobKind(NOTIFY_KIND, NotifyInput, _notified), settings=lambda: current, prices=PRICES
        )
    )
    return await _run(chat, [NOTIFY_KIND], registry)


def digests(owner: uuid.UUID) -> list[tuple[date, str, str | None]]:
    rows = sql(
        "SELECT week_start, status, content FROM ai_digests WHERE owner_id = :o "
        "ORDER BY week_start",
        o=owner,
    ).all()
    return [(r.week_start, str(r.status), r.content) for r in rows]


def digest_jobs(owner: uuid.UUID) -> list[Any]:
    return sql(
        "SELECT id, state, origin, input, error_category FROM jobs WHERE owner_id = :o "
        "AND kind = 'ai_digest' ORDER BY created_at, id",
        o=owner,
    ).all()


def digest_mail(owner: uuid.UUID) -> list[tuple[str, str]]:
    rows = sql(
        "SELECT kind, route FROM mail_outbox WHERE owner_id = :o AND kind = 'ai_digest_ready'",
        o=owner,
    ).all()
    return [(str(r.kind), str(r.route)) for r in rows]
