"""The hourly purge (ADR 0003 section 1) and account deletion (PLAN section 4.3).

The worker calls ``purge_expired``. Account deletion and the "also remove imported
rows" option call the other functions here, after the Withings revoke. Owned rows
go with the account because each one cascades from it. Imported rows can also be
removed on their own. That option deletes the Withings row too, so the account can
be linked again. A plain disconnect keeps the row until the account goes.

The revoke itself runs in the worker (roadmap Amendment 1: web makes no Withings call but
the code exchange). The routes enqueue a ``withings_disconnect`` job; its handler here
(``disconnect_kind``) checks the step-up of the session named in the input again, then runs
``disconnect_withings``. The worker registers the kind (``ownvitals.worker.kinds``; guard 8
lists ``register_disconnect_kind`` among the entry points). The web side then finishes with
``connections.purge.purge_connection`` or ``delete_account``.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.admin_accounts import delete_user_account
from ownvitals.auth.qrlogin import expire_overdue, last_deadline
from ownvitals.auth.tables import (
    auth_events,
    auth_tokens,
    invites,
    known_devices,
    oauth_flows,
    pending_signups,
    pre_sessions,
    qr_logins,
    rate_limits,
    sessions,
    webauthn_challenges,
)
from ownvitals.config import Settings
from ownvitals.connections.purge import purge_connection, purge_webhook_events
from ownvitals.connections.repository import list_connections
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

# Owned tables that do not cascade from the account and must be removed by name.
# Empty: every owned table cascades, which the account-deletion test enumerates.
EXPLICIT_PURGE: tuple[str, ...] = ()
_PROVIDER = "withings"
RETENTION_DAYS = 30
DEVICE_RETENTION_DAYS = 90
# The longest rate-limit window is an hour; older windows can never be hit again.
RATE_LIMIT_KEEP = timedelta(hours=2)
QR_KEEP = timedelta(hours=1)  # a QR login row, after its last deadline (ADR 0011 section 5)
# ``sessions.PENDING_WINDOW``, repeated so the worker does not load the session module (a test
# holds them equal).
PENDING_WINDOW = timedelta(minutes=10)
# ``sessions.STEPUP_WINDOW`` (the largest ``stepup_window_minutes`` allows) and
# ``sessions.RECENT_AUTH_WINDOW``, repeated for the same reason (a test holds them equal).
STEPUP_WINDOW = timedelta(minutes=10)
RECENT_AUTH_WINDOW = timedelta(minutes=10)
# How much older than the route's check the proof may be when the job runs. The route waits
# far less than this for the job (``connections.routes.REVOKE_WAIT_SECONDS``).
JOB_SLACK = timedelta(minutes=2)
# The lease extension for the revoke calls of one job, and the retry deadline inside it.
DISCONNECT_DEADLINE_SECONDS = 300.0
_RETRY_SECONDS = 240.0
Sleep = Callable[[float], Awaitable[None]]


def _ago(days: int) -> Any:
    return func.now() - timedelta(days=days)


def _proof_at_or_before(now: Any) -> Any:
    """The later of ``reauth_at`` and ``stepup_at`` that is not after ``now``.

    A timestamp after ``now`` is not a proof: the same fail-closed lower bound as
    ``sessions._within``. ``GREATEST`` ignores nulls, so a future value dropped by ``CASE``
    leaves the other timestamp, and both missing is null (the caller uses ``created_at``).
    The same expression is in ``sessions.list_sessions``.
    """
    return func.greatest(
        case((sessions.c.reauth_at <= now, sessions.c.reauth_at)),
        case((sessions.c.stepup_at <= now, sessions.c.stepup_at)),
    )


async def purge_expired(
    factory: async_sessionmaker[AsyncSession], settings: Settings
) -> dict[str, int]:
    """Delete what is expired or used; returns the number of rows removed per table.

    - ``sessions``: past their absolute expiry, idle longer than the idle limit, or without a
      second factor ten minutes after their latest proof (a ``reauth_at`` or ``stepup_at``
      after now is not a proof); their ``ip`` is cleared after 30 days;
    - ``pending_signups``, ``auth_tokens`` (also used ones), ``oauth_flows``: expired;
    - ``pre_sessions`` and ``webauthn_challenges``: expired (ADR 0011 section 4);
    - ``qr_logins``: one hour after their last deadline (ADR 0011 section 5), so ``desktop_ip``
      and ``desktop_place`` never outlive it; live rows past their deadline are first set
      ``expired`` (``qr_login_expired`` for a scanned one), counted as ``qr_logins_expired``;
    - ``rate_limits``: windows over two hours old;
    - ``invites``: used or expired more than 30 days ago;
    - ``known_devices``: unused for 90 days;
    - ``auth_events``: older than 30 days.
    """
    idle = timedelta(hours=settings.session_idle_hours)
    now = func.now()
    statements = {
        "sessions": delete(sessions).where(
            or_(
                sessions.c.expires_at <= now,
                sessions.c.last_seen_at <= now - idle,
                # never passed its second factor, ten minutes after its latest proof.
                # A timestamp after now does not count.
                and_(
                    sessions.c.mfa_at.is_(None),
                    func.coalesce(_proof_at_or_before(now), sessions.c.created_at)
                    <= now - PENDING_WINDOW,
                ),
            )
        ),
        "pending_signups": delete(pending_signups).where(pending_signups.c.expires_at <= now),
        "auth_tokens": delete(auth_tokens).where(
            or_(auth_tokens.c.expires_at <= now, auth_tokens.c.used_at.is_not(None))
        ),
        "oauth_flows": delete(oauth_flows).where(oauth_flows.c.expires_at <= now),
        "webauthn_challenges": delete(webauthn_challenges).where(
            webauthn_challenges.c.expires_at <= now
        ),
        "pre_sessions": delete(pre_sessions).where(pre_sessions.c.expires_at <= now),
        "qr_logins": delete(qr_logins).where(last_deadline() <= now - QR_KEEP),
        "rate_limits": delete(rate_limits).where(
            rate_limits.c.window_start < now - RATE_LIMIT_KEEP
        ),
        "invites": delete(invites).where(
            or_(
                invites.c.used_at < _ago(RETENTION_DAYS),
                and_(invites.c.used_at.is_(None), invites.c.expires_at < _ago(RETENTION_DAYS)),
            )
        ),
        "known_devices": delete(known_devices).where(
            known_devices.c.last_used_at < _ago(DEVICE_RETENTION_DAYS)
        ),
        "auth_events": delete(auth_events).where(auth_events.c.at < _ago(RETENTION_DAYS)),
    }
    counts: dict[str, int] = {}
    async with system_session(factory) as db:
        counts["qr_logins_expired"] = await expire_overdue(db)
        for name, statement in statements.items():
            result = await db.execute(statement)
            counts[name] = int(result.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue]
        cleared = await db.execute(
            update(sessions)
            .where(sessions.c.ip.is_not(None), sessions.c.created_at < _ago(RETENTION_DAYS))
            .values(ip=None)
        )
        counts["sessions_ip_cleared"] = int(cleared.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue]
    return counts


async def delete_account(
    factory: async_sessionmaker[AsyncSession], ctx: UserContext, *, ip: str | None = None
) -> None:
    """Delete the account once the revoke ran. Webhook events first, while the connection
    rows still name them; every owned row then cascades with the account. The audit row is the
    account-deleted event: no measurement and no credential."""
    await purge_webhook_events(factory, ctx)
    async with system_session(factory) as db:
        await delete_user_account(db, ctx.user_id, ip=ip)


async def session_stepped_up(
    factory: async_sessionmaker[AsyncSession], ctx: UserContext, session_id: UUID
) -> bool:
    """Whether ``session_id`` is a live, full session of this user with a recent
    authentication and a step-up (``require_sensitive``), allowing ``JOB_SLACK`` for the time
    the job waited. A timestamp after now is not a proof.

    Narrower than ``sessions.resolve_session`` by design: it does not check the idle timeout
    or a disabled account (the issuer refuses a disabled owner before the handler runs), and
    "full" is ``mfa_at`` being set. Within ``JOB_SLACK`` it only gates a revoke, which
    protects the user's privacy rather than granting access."""
    now = func.now()
    query = select(sessions.c.id).where(
        sessions.c.id == session_id,
        sessions.c.user_id == ctx.user_id,
        sessions.c.expires_at > now,
        sessions.c.mfa_at.is_not(None),
        sessions.c.reauth_at <= now,
        sessions.c.reauth_at >= now - (RECENT_AUTH_WINDOW + JOB_SLACK),
        sessions.c.stepup_at <= now,
        sessions.c.stepup_at >= now - (STEPUP_WINDOW + JOB_SLACK),
    )
    async with system_session(factory) as db:
        return (await db.execute(query)).first() is not None


async def finish_disconnect(
    factory: async_sessionmaker[AsyncSession],
    ring: KeyRing,
    ctx: UserContext,
    connection_id: UUID,
    *,
    stepped_up: bool,
    delete_imported: bool = False,
    inner: object = None,
    budget: RetryBudget | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> DisconnectResult:
    """Revoke and clear credentials, then optionally remove imported rows and the rows derived
    from them (``connections.purge.IMPORTED``).

    The option that deletes imported data also deletes this connection row. A plain
    disconnect keeps the row.
    """
    result = await disconnect_withings(
        factory,
        ring,
        ctx,
        connection_id,
        stepped_up=stepped_up,
        inner=inner,
        budget=budget,
        sleep=sleep,
    )
    if delete_imported:
        await purge_connection(factory, ctx, connection_id)
    return result


async def delete_owner(
    factory: async_sessionmaker[AsyncSession],
    ring: KeyRing,
    ctx: UserContext,
    *,
    stepped_up: bool,
    ip: str | None = None,
    inner: object = None,
    budget: RetryBudget | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> DisconnectResult:
    """Revoke a live Withings grant, then delete the account (``delete_account``).

    In-process: the worker or a test. The route enqueues the revoke instead. A missing
    step-up deletes nothing.
    """
    if stepped_up is not True:
        raise DisconnectRefused("step_up_required")
    rows = await list_connections(factory, ctx)
    result = DisconnectResult(False, 0, False, False, None)
    for row in rows:
        if row.provider == _PROVIDER and row.status != "disconnected":
            result = await disconnect_withings(
                factory,
                ring,
                ctx,
                row.id,
                stepped_up=True,
                inner=inner,
                budget=budget,
                sleep=sleep,
            )
    await delete_account(factory, ctx, ip=ip)
    return result


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
