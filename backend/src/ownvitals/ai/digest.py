"""The weekly digest's job kind ``ai_digest`` and when it is queued (ADR 0014 sections 3, 5 and 7;
ADR 0010 section 8; PLAN section 7.4; issue 209). Worker only; web reads the stored digests
(``ai.digest_store``) and never calls the model.

**When.** ``then_digest`` wraps the findings notification pass (``notify_findings``) in the
worker's registry. That pass runs for one owner, in their context, after each rule evaluation of
the 6-hourly poll and the nightly reconciliation, so no cross-user query is needed:
``queue_if_due`` asks, for that owner only, whether a digest is due and then queues one
``ai_digest`` job (origin ``scheduler``, singleton ``ai_digest``, one attempt). Due means all of:
``ai_insights`` on in the environment and every owner lock open (``gates.check_locks``: with the
defaults nothing is ever queued), the stored flag and a live consent to the current text
(``consent.require_enabled``, the consent row ``FOR SHARE NOWAIT`` because the fenced session
already holds the job row), the digest opt-in, no digest stored for the week yet and none running.

**One run**, for the job's user only (``job.ctx``; every statement also names the owner):

1. The week must be one ``digest_model.week_ok`` admits, else ``invalid_input``. A week already
   stored, or an owner who opted out meanwhile, ends the job at once without a call.
2. The turn (``ai.turn.run_turn``, kind ``digest``, 2,048 output tokens): the client checks every
   lock, the flag, the consent and the budget before each call; tools read as the job's user.
3. ``Complete(store)``: the store reads the live consent ``FOR SHARE NOWAIT`` (a withdrawal that
   holds or has closed the row: nothing stored, the job ends ``cancelled``), then the opt-in
   (``FOR SHARE``; gone: nothing stored), and inserts the week. A ``ready`` digest also queues
   one ``ai_digest_ready`` outbox row (a fixed text, never the digest; ADR 0007). The cap stores
   the week as ``skipped_budget`` and a call that may have been billed as ``unavailable``
   (``digest_model.ending_for``), with no mail. A cancel requested meanwhile (a withdrawal)
   makes the runner cancel instead of storing.

Lock order, as in the chat turn: the job row (the fence) first, then the consent row without
waiting, then the opt-in and the digest; a withdrawal takes the consent row, then the job rows,
then deletes the digests and the opt-in.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ownvitals.ai import consent, digest_model
from ownvitals.ai.digest_model import DIGEST_KIND, SINGLETON_KEY, DigestInput
from ownvitals.ai.gates import ModelFailed, ModelRefused, check_locks
from ownvitals.ai.pricing import PRICES, Price
from ownvitals.ai.tables import DigestStatus
from ownvitals.ai.turn import run_turn
from ownvitals.config import Settings
from ownvitals.jobs import Complete, JobFailure, JobKind, KindRegistry
from ownvitals.jobs.errors import RunStopped
from ownvitals.jobs.fence import FencedSession
from ownvitals.jobs.kinds import Outcome
from ownvitals.jobs.run import JobRun
from ownvitals.notify.outbox import queue_user_mail

logger = logging.getLogger(__name__)

_OPTED_IN = text("SELECT 1 FROM ai_digest_prefs WHERE owner_id = :o")
_OPTED_IN_SHARE = text("SELECT 1 FROM ai_digest_prefs WHERE owner_id = :o FOR SHARE")
_STORED = text("SELECT 1 FROM ai_digests WHERE owner_id = :o AND week_start = :w")
_RUNNING = text(
    "SELECT 1 FROM jobs WHERE owner_id = :o AND kind = :kind AND state = 'running' LIMIT 1"
)
_INSERT = text(
    "INSERT INTO ai_digests (owner_id, week_start, status, content) "
    "VALUES (:o, :w, :status, :content) "
    "ON CONFLICT (owner_id, week_start) DO NOTHING RETURNING id"
)


async def _due(fs: FencedSession, owner: UUID, week: date) -> bool:
    """Opted in, and no digest stored for ``week``."""
    params = {"o": owner, "w": week}
    if (await fs.db.execute(_OPTED_IN, params)).first() is None:
        return False
    return (await fs.db.execute(_STORED, params)).first() is None


async def _queueable(fs: FencedSession, owner: UUID, week: date) -> bool:
    """``_due`` and no digest job running: a queued one collapses into the singleton, a running
    one does not, and a second job could bill the same week twice. (The run itself uses
    ``_due`` only: it is the running job.)"""
    if not await _due(fs, owner, week):
        return False
    return (await fs.db.execute(_RUNNING, {"o": owner, "kind": DIGEST_KIND})).first() is None


async def queue_if_due(
    job: JobRun,
    connection_id: UUID,
    settings: Settings,
    *,
    now: datetime | None = None,
    prices: Mapping[str, Price] = PRICES,
) -> bool:
    """Queue the owner's digest for the last full week when it is due (the module's "When").
    Best-effort: a database error is logged by its class name only and the next pass asks
    again. True when a job was queued (or a queued one was found)."""
    try:
        check_locks(settings, prices)
    except ModelRefused:
        return False
    week = digest_model.digest_week(now or datetime.now(UTC))
    owner = job.ctx.user_id
    try:
        try:
            async with job.fenced() as fs:
                await consent.require_enabled(fs.db, owner, settings, nowait=True)
                due = await _queueable(fs, owner, week)
        except consent.AiNotEnabled:
            return False
        if not due:
            return False
        from ownvitals.jobs.queue import enqueue

        await enqueue(
            job.factory,
            connection_id=connection_id,
            kind=DIGEST_KIND,
            origin="scheduler",
            payload=json.dumps({"week_start": week.isoformat()}),
            singleton_key=SINGLETON_KEY,
            max_attempts=1,  # each try may be billed
            target_secret_id=None,
        )
    except SQLAlchemyError as exc:
        logger.warning("digest not queued", extra={"error_type": type(exc).__name__})
        return False
    return True


def then_digest(
    kind: JobKind,
    *,
    settings: Callable[[], Settings] = Settings,
    prices: Mapping[str, Price] = PRICES,
) -> JobKind:
    """``kind`` (the notification pass) followed by ``queue_if_due`` after each successful
    attempt. Its input must carry the ``connection_id`` the job is queued for; ``settings`` and
    ``prices`` are the process's (the tests pass their own)."""

    async def run(job: JobRun) -> Outcome:
        outcome = await kind.run(job)
        connection = getattr(job.input, "connection_id", None)
        if isinstance(connection, UUID):
            await queue_if_due(job, connection, settings(), prices=prices)
        return outcome

    return JobKind(kind.name, kind.input_model, run)


def _store(
    owner: UUID, week: date, status: DigestStatus, content: str | None
) -> Callable[[FencedSession], Awaitable[None]]:
    async def store(fs: FencedSession) -> None:
        try:
            await consent.require_consent(fs.db, owner, nowait=True)
        except consent.AiNotEnabled as refused:
            if refused.reason == "no_consent":  # withdrawn, or a withdrawal holds the row
                raise RunStopped("cancelled") from None
            raise JobFailure("ai_not_enabled") from None
        params = {"o": owner, "w": week}
        if (await fs.db.execute(_OPTED_IN_SHARE, params)).first() is None:
            return  # opted out meanwhile: nothing is kept
        inserted = await fs.db.execute(_INSERT, {**params, "status": status, "content": content})
        if inserted.first() is not None and status == "ready":
            await queue_user_mail(fs.db, owner, "ai_digest_ready", "assistant")

    return store


async def run_digest(
    job: JobRun,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    **call_options: Any,
) -> Complete:
    """Write the week's digest for the job's user; ``call_options`` reach the model client (the
    tests' fake transport and prices)."""
    parsed = job.input
    moment = now or datetime.now(UTC)
    if not isinstance(parsed, DigestInput) or not digest_model.week_ok(parsed.week_start, moment):
        raise JobFailure("invalid_input")
    settings = settings if settings is not None else Settings()
    owner, week = job.ctx.user_id, parsed.week_start
    async with job.fenced() as fs:
        due = await _due(fs, owner, week)
    if not due:
        return Complete()
    try:
        turn = await run_turn(
            job,
            settings,
            kind="digest",
            history=digest_model.request(week),
            now=moment,
            **call_options,
        )
    except (ModelRefused, ModelFailed) as error:
        end = digest_model.ending_for(error.code)
        if end.ending == "stop":
            await job.checkpoint()  # a cancel requested meanwhile stops the run here
            raise JobFailure("ai_unavailable") from None
        if end.ending != "store" or end.status is None:
            raise JobFailure(end.ending) from None
        return Complete(_store(owner, week, end.status, None))
    if turn.stopped != "answered" or turn.text is None or not digest_model.content_fits(turn.text):
        return Complete(_store(owner, week, "unavailable", None))
    return Complete(_store(owner, week, "ready", turn.text))


def register_digest_kind(registry: KindRegistry) -> JobKind:
    """``ai_digest`` in the worker's registry (never the API's: web never queues it)."""
    return registry.register(JobKind(DIGEST_KIND, DigestInput, run_digest))
