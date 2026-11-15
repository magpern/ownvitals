"""The ``withings_disconnect`` job kind (roadmap Amendment 1): the revoke runs in the worker.

The routes enqueue the job; ``execute_disconnect`` checks the step-up of the session named in
the input again (``session_stepped_up``), then runs ``disconnect_withings``. The worker
registers the kind through ``ownvitals.auth.purge.register_disconnect_kind`` (guard 8 lists
that name among the entry points), which re-exports it from here.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.session_full_sql import owes_no_second_factor
from ownvitals.auth.tables import sessions
from ownvitals.config import Settings
from ownvitals.context import UserContext
from ownvitals.crypto.keks import KeyRing, KeyRingError, load_keyring
from ownvitals.db import system_session
from ownvitals.jobs.errors import JobFailure
from ownvitals.jobs.kinds import Complete, JobKind, KindRegistry
from ownvitals.jobs.run import JobRun
from ownvitals.withings.disconnect import (
    DISCONNECT_KIND,
    DisconnectInput,
    DisconnectRefused,
    DisconnectResult,
    disconnect_withings,
)
from ownvitals.withings.errors import RetryBudget

# ``sessions.STEPUP_WINDOW`` (the largest ``stepup_window_minutes`` allows) and
# ``sessions.RECENT_AUTH_WINDOW``, repeated so the worker does not load the session module (a
# test holds them equal).
STEPUP_WINDOW = timedelta(minutes=10)
RECENT_AUTH_WINDOW = timedelta(minutes=10)
# How much older than the route's check the proof may be when the job runs. The route waits
# far less than this for the job (``connections.routes.REVOKE_WAIT_SECONDS``).
JOB_SLACK = timedelta(minutes=2)
# The lease extension for the revoke calls of one job, and the retry deadline inside it.
DISCONNECT_DEADLINE_SECONDS = 300.0
_RETRY_SECONDS = 240.0
Sleep = Callable[[float], Awaitable[None]]


async def session_stepped_up(
    factory: async_sessionmaker[AsyncSession], ctx: UserContext, session_id: UUID
) -> bool:
    """Whether ``session_id`` is a live, full session of this user with a recent
    authentication and a step-up (``require_sensitive``), allowing ``JOB_SLACK`` for the time
    the job waited. A timestamp after now is not a proof.

    Narrower than ``sessions.resolve_session`` by design: it does not check the idle timeout
    or a disabled account (the issuer refuses a disabled owner before the handler runs), and
    "full" is ``session_full_sql.owes_no_second_factor`` without ``OWNVITALS_ADMIN_EMAILS``
    (#475: the stored role decides here; the route's ``require_sensitive`` already refused a
    listed address without TOTP before the job existed).
    Within ``JOB_SLACK`` it only gates a revoke, which protects the user's privacy rather than
    granting access."""
    now = func.now()
    query = select(sessions.c.id).where(
        sessions.c.id == session_id,
        sessions.c.user_id == ctx.user_id,
        sessions.c.expires_at > now,
        owes_no_second_factor(()),
        sessions.c.reauth_at <= now,
        sessions.c.reauth_at >= now - (RECENT_AUTH_WINDOW + JOB_SLACK),
        sessions.c.stepup_at <= now,
        sessions.c.stepup_at >= now - (STEPUP_WINDOW + JOB_SLACK),
    )
    async with system_session(factory) as db:
        return (await db.execute(query)).first() is not None


async def _worker_ring(job: JobRun) -> KeyRing:
    try:
        return await load_keyring(job.factory, Settings(), role="worker")
    except KeyRingError:
        raise JobFailure("key_unavailable") from None


async def execute_disconnect(
    job: JobRun,
    ring: KeyRing,
    *,
    inner: object = None,
    budget: RetryBudget | None = None,
    sleep: Sleep | None = None,
) -> DisconnectResult:
    """One ``withings_disconnect`` run: the step-up check, then the revoke-first path.

    A session that is not this owner's, or whose proof is too old, fails the run
    (``invalid_input``) and nothing is revoked or cleared: the route clears the credentials
    itself and reports the revoke as failed. A row that is not live fails ``not_connected``.
    """
    parsed = job.input
    if not isinstance(parsed, DisconnectInput):
        raise JobFailure("invalid_input")
    if not await session_stepped_up(job.factory, job.ctx, parsed.session_id):
        raise JobFailure("invalid_input")
    limit = budget or RetryBudget(retries=2, deadline=time.monotonic() + _RETRY_SECONDS)
    refused: str | None = None
    result: DisconnectResult | None = None
    async with job.provider_call(DISCONNECT_DEADLINE_SECONDS):
        try:
            result = await disconnect_withings(
                job.factory,
                ring,
                job.ctx,
                parsed.connection_id,
                stepped_up=True,
                inner=inner,
                budget=limit,
                sleep=sleep,
            )
        except DisconnectRefused as exc:
            refused = exc.reason
    if result is None:
        raise JobFailure("not_connected" if refused == "not_connected" else "invalid_input")
    return result


def disconnect_kind(
    *,
    ring: KeyRing | None = None,
    inner: object = None,
    budget: Callable[[], RetryBudget] | None = None,
    sleep: Sleep | None = None,
) -> JobKind:
    """The ``withings_disconnect`` kind. The worker passes nothing: the key ring is loaded per
    run as the worker. Tests pass a ring and the fake's transport."""

    async def run(job: JobRun) -> Complete:
        held = ring if ring is not None else await _worker_ring(job)
        await execute_disconnect(
            job, held, inner=inner, budget=budget() if budget else None, sleep=sleep
        )
        return Complete()

    return JobKind(DISCONNECT_KIND, DisconnectInput, run)


def register_disconnect_kind(registry: KindRegistry) -> None:
    """Register ``withings_disconnect`` on the worker's registry.

    ``ownvitals.worker.kinds.worker_registry`` calls this; guard 8 lists it in
    ``PUBLIC_ENTRY_POINTS`` (``tests/auth_tables_scan.py``)."""
    if registry.get(DISCONNECT_KIND) is None:
        registry.register(disconnect_kind())
