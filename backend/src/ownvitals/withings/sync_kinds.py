"""The sync engine's job kinds (PLAN section 4.4): the handlers of ``sync.py`` and
``sync_connect.py`` as the worker runs them.

Handlers are not attached to the API's registry (``KINDS``, which stays empty).
`register_sync_kinds` registers the five kinds on the registry it is given; the worker's registry
(``ownvitals.worker.kinds``) is built with it. Each handler opens the owner's grant and the app's
client in the job's context (`open_materials`), then runs its pass.
"""

from uuid import UUID

from ownvitals.derived.normalize_job import TRIGGERS, enqueue_normalize
from ownvitals.ingest.calls import Account
from ownvitals.jobs import Complete, JobFailure, JobKind, KindRegistry
from ownvitals.jobs.run import JobRun
from ownvitals.jobs.withings import ConnectInput, StreamInput
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.sync import execute_backfill, execute_sync
from ownvitals.withings.sync_connect import execute_connect, execute_poll


def register_sync_kinds(registry: KindRegistry) -> None:
    """Register this issue's five kinds. Other Withings kinds stay unregistered."""
    owned = (
        ("withings_connect", ConnectInput, run_connect),
        ("withings_backfill", StreamInput, run_backfill),
        ("withings_sync", StreamInput, run_sync_kind),
        ("ecg_fetch", StreamInput, run_ecg_fetch),
        ("withings_poll", ConnectInput, run_poll),
    )
    for name, model, handler in owned:
        if registry.get(name) is None:
            registry.register(JobKind(name, model, handler))


async def open_materials(job: JobRun, connection_id: UUID) -> tuple[Account, WithingsApp]:
    """The owner's grant and the app's client, read in the job's context. A missing key, a
    connection that is not usable or a decrypt failure is that job category."""
    from ownvitals.config import Settings
    from ownvitals.connections.errors import ConnectionFailure
    from ownvitals.connections.providers.app_secret import open_client_secret
    from ownvitals.connections.providers.withings import open_withings
    from ownvitals.crypto.keks import KeyRingError, load_keyring

    try:
        ring = await load_keyring(job.factory, Settings(), role="worker")
        view = await open_withings(job.factory, ring, job.ctx, connection_id)
        secret = await open_client_secret(job.factory, ring, job.ctx, int(view.public["app_id"]))
    except KeyRingError:
        raise JobFailure("key_unavailable") from None
    except ConnectionFailure as failure:
        category = (
            "not_connected" if failure.category == "blocked_by_provider_plan" else failure.category
        )
        if category not in {
            "needs_reconnect",
            "not_connected",
            "key_unavailable",
            "decrypt_failed",
        }:
            category = "internal"
        raise JobFailure(category) from None
    raw = view.public.get("scopes") or ()
    if isinstance(raw, str):
        raw = [part for part in raw.split(",") if part]
    scopes = tuple(str(item) for item in raw) if isinstance(raw, list | tuple) else ()
    account = Account(connection_id, int(view.public["app_id"]), scopes, "UTC", view.access_token)
    return account, WithingsApp(secret.client_id, secret.secret)


async def run_backfill(job: JobRun) -> Complete:
    parsed = job.input
    if not isinstance(parsed, StreamInput):
        raise JobFailure("invalid_input")
    account, app = await open_materials(job, parsed.connection_id)
    async with WithingsClient.open(app) as client:
        await execute_backfill(job, account, client, parsed.stream)
    if parsed.stream in TRIGGERS:
        await enqueue_normalize(job.factory, parsed.connection_id, "backfill")
    return Complete()


async def run_sync_kind(job: JobRun) -> Complete:
    parsed = job.input
    if not isinstance(parsed, StreamInput):
        raise JobFailure("invalid_input")
    account, app = await open_materials(job, parsed.connection_id)
    async with WithingsClient.open(app) as client:
        await execute_sync(job, account, client, parsed.stream)
    if parsed.stream in TRIGGERS:
        await enqueue_normalize(job.factory, parsed.connection_id, "live")
    return Complete()


async def run_ecg_fetch(job: JobRun) -> Complete:
    parsed = job.input
    if not isinstance(parsed, StreamInput):
        raise JobFailure("invalid_input")
    account, app = await open_materials(job, parsed.connection_id)
    async with WithingsClient.open(app) as client:
        await execute_sync(job, account, client, "ecg")
    return Complete()


async def run_connect(job: JobRun) -> Complete:
    parsed = job.input
    if not isinstance(parsed, ConnectInput):
        raise JobFailure("invalid_input")
    account, app = await open_materials(job, parsed.connection_id)
    async with WithingsClient.open(app) as client:
        await execute_connect(job, account, client)
    return Complete()


async def run_poll(job: JobRun) -> Complete:
    parsed = job.input
    if not isinstance(parsed, ConnectInput):
        raise JobFailure("invalid_input")
    account, app = await open_materials(job, parsed.connection_id)
    async with WithingsClient.open(app) as client:
        await execute_poll(job, account, client)
    return Complete()
