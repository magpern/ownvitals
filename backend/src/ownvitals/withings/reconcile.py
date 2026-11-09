"""Nightly reconciliation (PLAN section 4.4).

A measure row is deleted only after two complete misses; an error deletes nothing.
Measures are re-read through now + 3 months and no further. Callback URLs are never logged.
Subscription repair lives in ``reconcile_subscriptions.py``, the cursor in ``reconcile_cursor.py``.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from ownvitals.ingest.calls import Account, Classified, limited
from ownvitals.ingest.codec import payload_hash
from ownvitals.ingest.fetch import window
from ownvitals.ingest.revisions import bump, current
from ownvitals.ingest.rows import soft_delete_measure, upsert_measure_group
from ownvitals.ingest.when import plus_months
from ownvitals.jobs import JobFailure
from ownvitals.jobs.run import JobRun
from ownvitals.jobs.withings import lane_for
from ownvitals.withings.capabilities import NOT_AUTHORIZED, after_success, from_scopes
from ownvitals.withings.client import WithingsClient
from ownvitals.withings.errors import RetryBudget
from ownvitals.withings.reconcile_cursor import _load, _save
from ownvitals.withings.reconcile_drift import DriftTotals as DriftTotals
from ownvitals.withings.reconcile_drift import drift_totals as drift_totals
from ownvitals.withings.reconcile_drift import record_drift as _record
from ownvitals.withings.reconcile_subscriptions import RepairResult, repair_subscriptions
from ownvitals.withings.reconcile_subscriptions import (
    marked_for_resubscribe as marked_for_resubscribe,
)
from ownvitals.withings.streams import STREAMS, stream_named, windows

_LIVE = text(
    "SELECT grpid FROM measure_groups WHERE owner_id = :owner "
    "AND connection_id = :connection AND deleted_at IS NULL"
)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    stream: str
    complete: bool
    changed: int
    deleted: int
    capability: str | None = None


@dataclass(frozen=True, slots=True)
class _Pages:
    seen: set[str]
    changed: int
    zone: str | None
    empty: bool = False
    goals: dict[str, Any] | None = None


def _ids(raw: Any) -> set[str]:
    return {str(item) for item in raw} if isinstance(raw, list) else set()


async def _stop(
    job: JobRun, account: Account, name: str, capability: str | None
) -> ReconcileResult:
    async with job.fenced() as fs:
        cursor = await _load(fs.db, job.ctx.user_id, account.connection_id, name)
        kept = capability if capability is not None else cursor.capability
        await _save(
            fs.db, job.ctx.user_id, account.connection_id, name, cursor,
            capability=kept, state=cursor.state, zone=cursor.zone, last_ok=cursor.last_ok,
        )  # fmt: skip
    return ReconcileResult(name, False, 0, 0, capability)


async def _measures(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    lane: int,
    budget: RetryBudget,
    spans: list[tuple[int, int]],
    now: datetime,
) -> _Pages | Classified | None:
    seen: set[str] = set()
    changed = 0
    zone = account.zone
    for start, end in spans:
        await job.checkpoint()
        for category in (1, 2):
            offset = 0
            while True:
                body = await limited(
                    job,
                    account,
                    lane,
                    lambda offset=offset, category=category, start=start, end=end: client.getmeas(
                        account.access_token,
                        category=category,
                        startdate=start,
                        enddate=end,
                        offset=offset or None,
                        budget=budget,
                    ),
                )
                if isinstance(body, Classified):
                    return body
                zone = body.timezone or zone
                for group in body.measuregrps:
                    seen.add(str(int(group.grpid)))
                    async with job.fenced() as fs:
                        outcome = await upsert_measure_group(
                            fs.db, job.ctx.user_id, account.connection_id, group, zone, now
                        )
                    if outcome in {"inserted", "updated"}:
                        changed += 1
                if not body.more:
                    break
                nxt = int(body.offset)
                if nxt <= offset:
                    return None
                offset = nxt
                await job.checkpoint()
    return _Pages(seen, changed, zone, not seen)


async def _delete_missing(
    job: JobRun, account: Account, name: str, pages: _Pages, now: datetime
) -> ReconcileResult:
    owner = job.ctx.user_id
    async with job.fenced() as fs:
        stored = {
            str(int(item))
            for item in (
                await fs.db.execute(_LIVE, {"owner": owner, "connection": account.connection_id})
            ).scalars()
        }
        cursor = await _load(fs.db, owner, account.connection_id, name)
        missing = stored - pages.seen
        doomed = missing & _ids(cursor.state.get("reconcile_absent"))
        deleted = 0
        for grpid in sorted(doomed, key=int):
            if await soft_delete_measure(fs.db, owner, account.connection_id, int(grpid)):
                deleted += 1
        state = dict(cursor.state)
        state["reconcile_absent"] = sorted(missing - doomed, key=int)
        state["reconcile_changed"] = pages.changed
        state["reconcile_deleted"] = deleted
        capability = after_success(stream=name, empty=pages.empty, devices_known_empty=False)
        await _save(
            fs.db, owner, account.connection_id, name, cursor, capability=capability,
            state=state, zone=pages.zone, last_ok=now,
        )  # fmt: skip
    _record(account, name, pages.changed, deleted)
    return ReconcileResult(name, True, pages.changed, deleted, capability)


async def _other(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    name: str,
    shape: str,
    lane: int,
    budget: RetryBudget,
    spans: list[tuple[int, int]],
    now: datetime,
) -> _Pages | Classified:
    async with job.fenced() as fs:
        before = await current(fs.db, job.ctx.user_id, name)
    goals: dict[str, Any] | None = None
    zone, empty = account.zone, True
    for start, end in spans:
        await job.checkpoint()
        got = await window(job, account, client, lane, budget, shape, start, end, now)
        if isinstance(got, Classified):
            return got
        zone = got.zone or zone
        empty = empty and got.empty
        goals = got.goals if got.goals is not None else goals
    async with job.fenced() as fs:
        after = await current(fs.db, job.ctx.user_id, name)
    return _Pages(set(), after - before, zone, empty, goals)


async def _finish_other(
    job: JobRun, account: Account, name: str, pages: _Pages, now: datetime
) -> ReconcileResult:
    owner, changed = job.ctx.user_id, pages.changed
    async with job.fenced() as fs:
        cursor = await _load(fs.db, owner, account.connection_id, name)
        state = dict(cursor.state)
        if pages.goals is not None:
            digest = payload_hash(pages.goals).hex()
            if state.get("goals_hash") != digest:
                state["goals"], state["goals_hash"] = pages.goals, digest
                await bump(fs.db, owner, "goals")
                changed += 1
        state["reconcile_changed"] = changed
        state["reconcile_deleted"] = 0
        devices = await _load(fs.db, owner, account.connection_id, "devices")
        capability = after_success(
            stream=name,
            empty=pages.empty,
            devices_known_empty=devices.state.get("no_devices") is True,
        )
        await _save(
            fs.db, owner, account.connection_id, name, cursor, capability=capability,
            state=state, zone=pages.zone, last_ok=now,
        )  # fmt: skip
    _record(account, name, changed, 0)
    return ReconcileResult(name, True, changed, 0, capability)


async def execute_reconcile(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    name: str,
    *,
    lane: int | None = None,
    now: datetime | None = None,
) -> ReconcileResult:
    try:
        spec = stream_named(name)
    except KeyError:
        raise JobFailure("invalid_input") from None
    moment = now or datetime.now(UTC)
    lane = lane_for("withings_reconcile", "scheduler") if lane is None else lane
    if from_scopes(spec, account.scopes) == NOT_AUTHORIZED:
        return await _stop(job, account, name, NOT_AUTHORIZED)
    read_until = moment
    if spec.shape == "measures":
        # Inclusive API end is the last instant strictly before ingest's horizon.
        read_until = plus_months(moment, 3) - timedelta(microseconds=1)
    spans = windows(spec, read_until)
    if not spans:
        return await _stop(job, account, name, None)
    budget = RetryBudget(retries=2)
    if spec.shape == "measures":
        pages = await _measures(job, account, client, lane, budget, spans, moment)
    else:
        pages = await _other(job, account, client, name, spec.shape, lane, budget, spans, moment)
    if pages is None or isinstance(pages, Classified):
        capability = None if pages is None else pages.capability
        return await _stop(job, account, name, capability)
    if spec.shape != "measures":
        return await _finish_other(job, account, name, pages, moment)
    return await _delete_missing(job, account, name, pages, moment)


async def execute_nightly(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    *,
    callback_base: str | None = None,
    current_url: str | None = None,
    resubscribe: bool = False,
    streams: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> tuple[list[ReconcileResult], RepairResult | None]:
    """Every stream, then subscription repair when the current callback is known."""
    names = streams if streams is not None else tuple(spec.name for spec in STREAMS)
    results = [await execute_reconcile(job, account, client, name, now=now) for name in names]
    repair = None
    if callback_base is not None and current_url is not None:
        repair = await repair_subscriptions(
            job,
            account,
            client,
            callback_base=callback_base,
            current_url=current_url,
            resubscribe=resubscribe,
        )
    return results, repair
