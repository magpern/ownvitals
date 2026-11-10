"""The worker's job kinds, its webhook-target check and its schedule (PLAN section 4.4, ADR 0005
section 8, ADR 0008).

``worker_registry()`` builds the registry the worker process claims from: the five sync kinds of
``ownvitals.withings.sync`` (``withings_connect``, ``withings_backfill``, ``withings_sync``,
``ecg_fetch``, ``withings_poll``), the nightly ``withings_reconcile``,
``token_refresh_recover`` and ``withings_disconnect`` (the revoke a disconnect or an account
deletion enqueues, ``ownvitals.auth.purge``, #163), and the analysis chain
``derive_normalize``, ``derive_daily``, ``derive_evaluate`` and ``derive_scores`` (#185, #186,
#187, #188), the ECG ``estimated`` recompute ``ecg_recompute`` (#191; the nightly
reconciliation queues it as the sweep after a module version bump, ADR 0006 section 15), and the
finding notifications ``notify_findings`` (#195), queued after each evaluation and recompute,
and the user's JSON/CSV export ``export_build`` (#211, ADR 0013 section 4), and the chat
assistant's ``ai_chat_turn`` (#208, ADR 0014 section 5; its model calls need AI on), and the
weekly digest ``ai_digest`` (issue 209), queued for its owner by the notification pass
(``then_digest``) when one is due. Only
``python -m ownvitals.worker`` builds it. The API's registry (``ownvitals.jobs.KINDS``) stays
empty, so ``POST /api/jobs`` still refuses every kind and the web process registers no handler
(web imports ``ownvitals.auth.purge`` for the routes, which defines the disconnect handler but
registers it nowhere).

``WORKER_TARGETS`` is the target re-check the runner asks before a job that names a webhook
secret (``ownvitals.withings.webhooks.WebhookTargets``: the entry must exist, be open and not be
retired). ``SCHEDULES`` is what the worker loop hands to ``queue_schedule``: a poll every six
hours and a reconciliation every 24 hours per connected connection.

The reconciliation's subscription repair needs the current callback URL. It is derived here, in
the worker, from the newest open entry of the app's ``webhook_secrets`` and its seed (ADR 0004
section 4, ADR 0008), passed to ``execute_nightly`` and dropped: never logged, stored or put on a
job. With no open entry, or no seed to derive it, the reconciliation runs without the repair.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from ownvitals.ai.assistant import register_chat_kind
from ownvitals.ai.digest import register_digest_kind, then_digest
from ownvitals.auth.purge import register_disconnect_kind
from ownvitals.crypto.keks import KeyRing, KeyRingError, KeyUnavailable
from ownvitals.derived.daily_job import register_daily_kind
from ownvitals.derived.ecg_recompute import enqueue_recompute, register_recompute_kind
from ownvitals.derived.evaluate_job import register_evaluate_kind
from ownvitals.derived.normalize_job import enqueue_normalize, register_normalize_kind
from ownvitals.derived.scores_job import register_scores_kind
from ownvitals.jobs import Complete, JobFailure, JobKind, KindRegistry
from ownvitals.jobs.run import JobRun
from ownvitals.jobs.withings import ConnectInput, SecretTargets
from ownvitals.notify.findings_job import register_notify_kind, then_notify
from ownvitals.reports.doctor import register_doctor_kind
from ownvitals.reports.export import register_export_kind
from ownvitals.withings.client import WithingsClient
from ownvitals.withings.reconcile import execute_nightly
from ownvitals.withings.subscriptions import callback_url, derive_secret
from ownvitals.withings.sync_kinds import open_materials, register_sync_kinds
from ownvitals.withings.tokens import recover_refresh
from ownvitals.withings.webhooks import WebhookTargets

logger = logging.getLogger(__name__)

# PLAN section 4.4: the poll is a 6-hourly safety net, the reconciliation nightly.
SCHEDULES: Final[dict[str, int]] = {
    "withings_poll": 6 * 3600,
    "withings_reconcile": 24 * 3600,
}
# One ``requesttoken`` (30 s) plus the refresh lock and the fenced writes around it.
RECOVER_DEADLINE_SECONDS: Final = 60.0

WORKER_TARGETS: SecretTargets = WebhookTargets()

_APP = text("SELECT callback_base, webhook_secrets FROM public.withings_apps WHERE id = :id")
_RECOVER_FAILURES: Final = frozenset({"needs_reconnect", "not_connected"})


async def _ring(job: JobRun) -> KeyRing:
    from ownvitals.config import Settings
    from ownvitals.crypto.keks import load_keyring

    try:
        return await load_keyring(job.factory, Settings(), role="worker")
    except KeyRingError:
        raise JobFailure("key_unavailable") from None


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _open_entry(raw: Any, now: datetime) -> tuple[UUID, str] | None:
    """``(secret_id, seed_id)`` of the newest entry that is open at ``now`` and not retired."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    best: tuple[datetime, UUID, str] | None = None
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict) or item.get("retire_at") is not None:
            continue
        opened = _when(item.get("active_from"))
        seed_id = item.get("seed_id")
        try:
            secret_id = UUID(str(item.get("secret_id")))
        except ValueError:
            continue
        if opened is None or opened > now or not isinstance(seed_id, str):
            continue
        if best is None or opened > best[0]:
            best = (opened, secret_id, seed_id)
    return None if best is None else (best[1], best[2])


async def current_callback(
    job: JobRun, ring: KeyRing, app_id: int, *, now: datetime | None = None
) -> tuple[str | None, str | None]:
    """``(callback_base, current callback URL)`` of the app; the URL is ``None`` when no entry
    is open or its seed is not available. The URL holds the secret: the caller passes it to
    Withings and drops it."""
    moment = now or datetime.now(UTC)
    async with job.fenced() as fs:
        row = (await fs.db.execute(_APP, {"id": app_id})).first()
        if row is None:
            return None, None
        base = str(row.callback_base)
        entry = _open_entry(row.webhook_secrets, moment)
        if entry is None:
            return base, None
        secret_id, seed_id = entry
        try:
            seed = await ring.seed_for(fs.db, seed_id)
        except KeyUnavailable:
            seed = None
    if seed is None:
        logger.warning("webhook seed unavailable", extra={"app_id": app_id})
        return base, None
    return base, callback_url(base, derive_secret(seed.key, app_id, secret_id))


async def run_reconcile(job: JobRun) -> Complete:
    """Every stream re-read, then subscription repair when the current callback is known."""
    parsed = job.input
    if not isinstance(parsed, ConnectInput):
        raise JobFailure("invalid_input")
    account, app = await open_materials(job, parsed.connection_id)
    base, current = await current_callback(job, await _ring(job), account.app_id)
    async with WithingsClient.open(app) as client:
        await execute_nightly(job, account, client, callback_base=base, current_url=current)
    await enqueue_normalize(job.factory, parsed.connection_id, "live")
    await enqueue_recompute(job.factory, parsed.connection_id)  # the sweep (section 15)
    return Complete()


async def run_token_recover(job: JobRun) -> Complete:
    """The one recovery try of PLAN section 4.3 step 4, with the stored refresh token."""
    parsed = job.input
    if not isinstance(parsed, ConnectInput):
        raise JobFailure("invalid_input")
    _, app = await open_materials(job, parsed.connection_id)
    ring = await _ring(job)
    async with WithingsClient.open(app) as client, job.provider_call(RECOVER_DEADLINE_SECONDS):
        result = await recover_refresh(
            job.factory, ring, job.ctx, parsed.connection_id, client, lease=job.lease
        )
    if result.outcome in ("refreshed", "current"):
        return Complete()
    raise JobFailure(result.outcome if result.outcome in _RECOVER_FAILURES else "internal")


def worker_registry() -> KindRegistry:
    """A new registry with every Withings kind the worker runs (PLAN section 4.4)."""
    registry = KindRegistry()
    register_sync_kinds(registry)
    registry.register(JobKind("withings_reconcile", ConnectInput, run_reconcile))
    registry.register(JobKind("token_refresh_recover", ConnectInput, run_token_recover))
    register_disconnect_kind(registry)
    register_normalize_kind(registry)
    register_daily_kind(registry)
    register_scores_kind(registry)
    # #195: a notification pass after each rule evaluation and each estimated recompute.
    followed = KindRegistry()
    register_evaluate_kind(followed)
    register_recompute_kind(followed)
    for kind in followed:
        registry.register(then_notify(kind))
    # Issue 209: each notification pass asks whether its owner's weekly digest is due.
    notify = KindRegistry()
    register_notify_kind(notify)
    for kind in notify:
        registry.register(then_digest(kind))
    register_export_kind(registry)  # #211: the user's JSON/CSV export (ADR 0013 section 4)
    register_doctor_kind(registry)  # issue 210: the doctor PDF (ADR 0013 section 6)
    register_chat_kind(registry)  # #208: the chat assistant's turn (ADR 0014 section 5)
    register_digest_kind(registry)  # issue 209: the weekly digest (ADR 0014 section 5)
    return registry
