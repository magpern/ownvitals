"""The ``withings_admin_revoke`` job kind (#365, PLAN section 4.3, ADR 0002 section 3).

An admin's account delete revokes the target's Withings grant first, as the user's own delete
does. The admin web path never holds the target's context, so ``admin_account_revoke`` (an
audited admin transaction) queues this job through ``queue_enqueue_admin_revoke``, naming the
audit row's admin and the admin's session; the job issuer gives it the target's context when
it is claimed. The kind is registered with ``disabled_owner=True``, so a target the admin
disabled first is still issued (ADR 0005 section 4, amended 2026-11-11, #383); a deleted one
fails ``account_disabled``.

``execute_admin_revoke`` first checks that session again (``admin_stepped_up``): a live, full
session of that admin, who is still an admin and not disabled, whose TOTP was entered in it
(not on a QR approval's behalf, as ``SessionInfo.role``), with a step-up inside the admin
write window plus ``JOB_SLACK``. Otherwise it fails ``invalid_input`` and revokes nothing. It
then runs the user's own revoke-first disconnect on each live Withings row of the target, and
fails ``upstream_unavailable`` when any revoke did not finish (the job has one attempt, so it is
dead-lettered: the admin delete reads that as ``failed``). The worker registers the kind through
``register_admin_revoke_kind`` (guard 8 lists it among the entry points).
"""

import time
from collections.abc import Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.disconnect_job import DISCONNECT_DEADLINE_SECONDS, JOB_SLACK, Sleep
from ownvitals.auth.session_model import ADMIN_MFA_WINDOW, ADMIN_STEPUP_WINDOW
from ownvitals.auth.tables import sessions, users
from ownvitals.config import Settings
from ownvitals.connections.repository import list_connections
from ownvitals.crypto.keks import KeyRing, KeyRingError, load_keyring
from ownvitals.db import system_session
from ownvitals.jobs.errors import JobFailure
from ownvitals.jobs.kinds import Complete, JobKind, KindRegistry
from ownvitals.jobs.run import JobRun
from ownvitals.withings.disconnect import DisconnectRefused, disconnect_withings
from ownvitals.withings.errors import RetryBudget

# The kind ``queue_enqueue_admin_revoke`` (20-queue.sql) inserts; a test holds them equal.
ADMIN_REVOKE_KIND = "withings_admin_revoke"
_PROVIDER = "withings"
_RETRY_SECONDS = 240.0
Factory = async_sessionmaker[AsyncSession]


class AdminRevokeInput(BaseModel):
    """The job's input, exactly as the definer builds it: ids only, no credential."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    admin_id: UUID
    session_id: UUID


async def admin_stepped_up(factory: Factory, admin_id: UUID, session_id: UUID) -> bool:
    """Whether ``session_id`` is a live session of ``admin_id`` that passed its second factor
    within the admin window and stepped up within the admin write window, both allowing
    ``JOB_SLACK`` for the time the job waited, and the account is an admin that is not
    disabled. A second factor entered on the approving phone for a QR login does not count
    (``mfa_by_approval``, as ``SessionInfo.role``). A timestamp after now is not a proof (the
    fail-closed lower bound of ``session_model._within``)."""
    now = func.now()
    query = (
        select(sessions.c.id)
        .join(users, users.c.id == sessions.c.user_id)
        .where(
            sessions.c.id == session_id,
            sessions.c.user_id == admin_id,
            sessions.c.expires_at > now,
            sessions.c.mfa_by_approval.is_(False),
            sessions.c.mfa_at <= now,
            sessions.c.mfa_at >= now - (ADMIN_MFA_WINDOW + JOB_SLACK),
            sessions.c.stepup_at <= now,
            sessions.c.stepup_at >= now - (ADMIN_STEPUP_WINDOW + JOB_SLACK),
            users.c.role == "admin",
            users.c.disabled_at.is_(None),
        )
    )
    async with system_session(factory) as db:
        return (await db.execute(query)).first() is not None


async def _worker_ring(job: JobRun) -> KeyRing:
    try:
        return await load_keyring(job.factory, Settings(), role="worker")
    except KeyRingError:
        raise JobFailure("key_unavailable") from None


async def execute_admin_revoke(
    job: JobRun,
    ring: KeyRing,
    *,
    inner: object = None,
    budget: RetryBudget | None = None,
    sleep: Sleep | None = None,
) -> int:
    """One run: the admin's step-up check, then the revoke-first disconnect of each live
    Withings row of the job's owner. Returns how many rows were revoked.

    A row another path disconnected meanwhile (``not_connected``) is skipped. Any revoke that
    did not finish fails the run ``upstream_unavailable``; the credentials are cleared anyway
    (``disconnect_withings``) and go with the account."""
    parsed = job.input
    if not isinstance(parsed, AdminRevokeInput):
        raise JobFailure("invalid_input")
    if not await admin_stepped_up(job.factory, parsed.admin_id, parsed.session_id):
        raise JobFailure("invalid_input")
    rows = await list_connections(job.factory, job.ctx)
    live = [row.id for row in rows if row.provider == _PROVIDER and row.status != "disconnected"]
    limit = budget or RetryBudget(retries=2, deadline=time.monotonic() + _RETRY_SECONDS)
    revoked = 0
    failed = False
    for connection_id in live:
        async with job.provider_call(DISCONNECT_DEADLINE_SECONDS):
            try:
                result = await disconnect_withings(
                    job.factory,
                    ring,
                    job.ctx,
                    connection_id,
                    stepped_up=True,
                    inner=inner,
                    budget=limit,
                    sleep=sleep,
                )
            except DisconnectRefused:
                continue
        if result.revoke_failed:
            failed = True
        else:
            revoked += 1
    if failed:
        raise JobFailure("upstream_unavailable")
    return revoked


def admin_revoke_kind(
    *,
    ring: KeyRing | None = None,
    inner: object = None,
    budget: Callable[[], RetryBudget] | None = None,
    sleep: Sleep | None = None,
) -> JobKind:
    """The ``withings_admin_revoke`` kind. The worker passes nothing: the key ring is loaded
    per run as the worker. Tests pass a ring and the fake's transport."""

    async def run(job: JobRun) -> Complete:
        held = ring if ring is not None else await _worker_ring(job)
        limit = budget() if budget else None
        await execute_admin_revoke(job, held, inner=inner, budget=limit, sleep=sleep)
        return Complete()

    return JobKind(ADMIN_REVOKE_KIND, AdminRevokeInput, run, disabled_owner=True)


def register_admin_revoke_kind(registry: KindRegistry) -> None:
    """Register ``withings_admin_revoke`` on the worker's registry.

    ``ownvitals.worker.kinds.worker_registry`` calls this; guard 8 lists it in
    ``PUBLIC_ENTRY_POINTS`` (``tests/auth_tables_scan.py``)."""
    if registry.get(ADMIN_REVOKE_KIND) is None:
        registry.register(admin_revoke_kind())
