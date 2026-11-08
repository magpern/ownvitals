"""The sync engine's connect and poll passes (PLAN section 4.4; the engine is ``sync.py``).

`withings_connect` reads devices, goals and capabilities, lists subscriptions and enqueues one
backfill per stream the grant allows. `withings_poll` enqueues a backfill for a stream that has
not finished and an incremental sync for one that has.
"""

import json
from datetime import UTC, datetime
from typing import Any

from ownvitals.ingest.calls import Account, Classified, limited
from ownvitals.ingest.codec import payload_hash
from ownvitals.ingest.fetch import once
from ownvitals.jobs.run import JobRun
from ownvitals.jobs.withings import singleton_for
from ownvitals.withings.capabilities import NOT_AUTHORIZED, after_success, from_scopes
from ownvitals.withings.client import WithingsClient
from ownvitals.withings.errors import RetryBudget
from ownvitals.withings.streams import STREAMS
from ownvitals.withings.sync import _lane, _owner_cursor, _store


async def _enqueue(job: JobRun, account: Account, kind: str, name: str | None) -> None:
    from ownvitals.jobs.queue import enqueue

    payload: dict[str, str] = {"connection_id": str(account.connection_id)}
    if name is not None:
        payload["stream"] = name
    key = singleton_for(account.connection_id, name) if kind == "withings_sync" and name else None
    await enqueue(
        job.factory,
        connection_id=account.connection_id,
        kind=kind,
        origin="scheduler",
        payload=json.dumps(payload),
        singleton_key=key,
        max_attempts=3,
        target_secret_id=None,
    )


async def execute_connect(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    *,
    lane: int | None = None,
    now: datetime | None = None,
) -> None:
    """Devices, goals, a capability per stream, the subscription list, then backfill jobs."""
    moment = now or datetime.now(UTC)
    lane = _lane("withings_connect") if lane is None else lane
    budget = RetryBudget(retries=2)
    for shape, stream in (("devices", "devices"), ("goals", "goals")):
        got = await once(job, account, client, lane, budget, shape, moment)
        capability = (
            got.capability
            if isinstance(got, Classified)
            else after_success(stream=stream, empty=got.empty, devices_known_empty=False)
        )
        skipped = 0 if isinstance(got, Classified) else got.skipped
        state: dict[str, Any] = {
            "status": "complete",
            "index": 1,
            "skipped_out_of_range": skipped,
        }
        if not isinstance(got, Classified) and got.no_devices is not None:
            state["no_devices"] = got.no_devices
        goals_changed = False
        if not isinstance(got, Classified) and got.goals is not None:
            digest = payload_hash(got.goals).hex()
            previous = await _owner_cursor(job, account, stream)
            goals_changed = previous["state"].get("goals_hash") != digest
            state["goals"] = got.goals
            state["goals_hash"] = digest
        await _store(
            job,
            account,
            stream,
            capability=capability,
            state=state,
            lastupdate=int(moment.timestamp()),
            zone=account.zone,
            covered_from=None,
            covered_to=None,
            done=True,
            bump_goals=goals_changed,
        )
    listed = await limited(
        job, account, lane, lambda: client.notify_list(account.access_token, budget=budget)
    )
    count = 0 if isinstance(listed, Classified) else len(listed.profiles)
    for spec in STREAMS:
        if spec.name in {"devices", "goals"}:
            continue
        capability = from_scopes(spec, account.scopes)
        state = {
            "status": "complete" if capability == NOT_AUTHORIZED else "running",
            "index": 0,
            "skipped_out_of_range": 0,
            "subscriptions": count,
        }
        await _store(
            job,
            account,
            spec.name,
            capability=capability,
            state=state,
            lastupdate=None,
            zone=account.zone,
            covered_from=None,
            covered_to=None,
            done=capability == NOT_AUTHORIZED,
        )
        if capability != NOT_AUTHORIZED:
            await _enqueue(job, account, "withings_backfill", spec.name)


async def execute_poll(job: JobRun, account: Account, client: WithingsClient) -> None:
    """Safety net: backfill what is unfinished, and sync what is already complete."""
    del client
    for spec in STREAMS:
        cursor = await _owner_cursor(job, account, spec.name)
        if cursor["state"].get("status") == "complete":
            await _enqueue(job, account, "withings_sync", spec.name)
        elif from_scopes(spec, account.scopes) != NOT_AUTHORIZED:
            await _enqueue(job, account, "withings_backfill", spec.name)
