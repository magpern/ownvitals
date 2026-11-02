"""Nightly reconciliation (PLAN section 4.4).

A measure row is deleted only after two complete misses; an error deletes nothing.
Measures are re-read through now + 3 months and no further. Callback URLs are never logged.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.ingest.calls import Account, Classified, limited
from ownvitals.ingest.codec import payload_hash
from ownvitals.ingest.cursors import write_cursor
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
from ownvitals.withings.streams import STREAMS, stream_named, windows
from ownvitals.withings.subscriptions import APPLIS

logger = logging.getLogger(__name__)

_APPLI_SCOPE = {
    1: "user.metrics", 2: "user.metrics", 4: "user.metrics", 16: "user.activity",
    44: "user.activity", 46: "user.info", 54: "user.metrics", 55: "user.metrics",
    58: "user.metrics", 62: "user.metrics", 63: "user.metrics",
}  # fmt: skip
_RESUBSCRIBE = "resubscribe"
_CHANGED = 0
_DELETED = 0
_LIVE = text(
    "SELECT grpid FROM measure_groups WHERE owner_id = :owner "
    "AND connection_id = :connection AND deleted_at IS NULL"
)
_CURSOR = text(
    "SELECT capability, lastupdate, backfill_state, source_tz, covered_from, covered_to, "
    "last_ok_at FROM sync_cursors WHERE owner_id = :owner AND connection_id = :connection "
    "AND stream = :stream"
)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    stream: str
    complete: bool
    changed: int
    deleted: int
    capability: str | None = None


@dataclass(frozen=True, slots=True)
class RepairResult:
    revoked: int
    updated: int
    subscribed: int


@dataclass(frozen=True, slots=True)
class DriftTotals:
    changed: int
    deleted: int


@dataclass
class _Cursor:
    capability: str | None
    lastupdate: int | None
    state: dict[str, Any]
    zone: str | None
    covered_from: datetime | None
    covered_to: datetime | None
    last_ok: datetime | None


@dataclass(frozen=True, slots=True)
class _Pages:
    seen: set[str]
    changed: int
    zone: str | None
    empty: bool = False
    goals: dict[str, Any] | None = None


def drift_totals() -> DriftTotals:
    return DriftTotals(_CHANGED, _DELETED)


def marked_for_resubscribe(public: Mapping[str, Any]) -> bool:
    return public.get(_RESUBSCRIBE) is True


def _note(changed: int, deleted: int) -> None:
    global _CHANGED, _DELETED
    _CHANGED += changed
    _DELETED += deleted


def _ids(raw: Any) -> set[str]:
    return {str(item) for item in raw} if isinstance(raw, list) else set()


def _under_base(url: str, base: str) -> bool:
    root = base.rstrip("/")
    parsed, base_parts = urlsplit(url), urlsplit(root)
    prefix = base_parts.path.rstrip("/") + "/"
    return (
        parsed.scheme == base_parts.scheme
        and parsed.netloc == base_parts.netloc
        and (parsed.path == base_parts.path or parsed.path.startswith(prefix))
    )


def _wanted(scopes: tuple[str, ...], applis: tuple[int, ...]) -> tuple[int, ...]:
    granted = set(scopes)
    return tuple(appli for appli in applis if _APPLI_SCOPE.get(appli) in granted)


async def _load(db: AsyncSession, owner: UUID, connection: UUID, stream: str) -> _Cursor:
    row = (
        await db.execute(_CURSOR, {"owner": owner, "connection": connection, "stream": stream})
    ).first()
    if row is None:
        return _Cursor(None, None, {}, None, None, None, None)
    state = row.backfill_state if isinstance(row.backfill_state, dict) else {}
    lastupdate = None if row.lastupdate is None else int(row.lastupdate)
    return _Cursor(
        row.capability, lastupdate, dict(state), row.source_tz, row.covered_from,
        row.covered_to, row.last_ok_at,
    )  # fmt: skip


async def _save(
    db: AsyncSession,
    owner: UUID,
    connection: UUID,
    stream: str,
    cursor: _Cursor,
    *,
    capability: str | None,
    state: dict[str, Any],
    zone: str | None,
    last_ok: datetime | None,
) -> None:
    await write_cursor(
        db, owner, connection, stream, capability=capability, state=state,
        lastupdate=cursor.lastupdate, covered_from=cursor.covered_from,
        covered_to=cursor.covered_to, last_ok=last_ok,
        zone=zone if zone is not None else cursor.zone,
    )  # fmt: skip


def _record(account: Account, stream: str, changed: int, deleted: int) -> None:
    _note(changed, deleted)
    logger.info(
        "withings reconciliation drift",
        extra={
            "connection_id": str(account.connection_id), "stream": stream,
            "changed": changed, "deleted": deleted,
        },
    )  # fmt: skip


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


async def _notify(
    job: JobRun, account: Account, lane: int, call: Callable[[], Awaitable[Any]]
) -> Any:
    result = await limited(job, account, lane, call)
    if isinstance(result, Classified):
        raise JobFailure("not_connected")
    return result


async def _revoke(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    lane: int,
    budget: RetryBudget,
    appli: int,
    url: str,
) -> None:
    await _notify(
        job,
        account,
        lane,
        lambda appli=appli, url=url: client.notify_revoke(
            account.access_token, callbackurl=url, appli=appli, budget=budget
        ),
    )


async def repair_subscriptions(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    *,
    callback_base: str,
    current_url: str,
    resubscribe: bool,
    applis: tuple[int, ...] = APPLIS,
    lane: int | None = None,
) -> RepairResult:
    """List, then revoke, update or subscribe. URLs are not stored and not logged."""
    lane = lane_for("withings_reconcile", "scheduler") if lane is None else lane
    budget = RetryBudget(retries=2)
    listed = await _notify(
        job, account, lane, lambda: client.notify_list(account.access_token, budget=budget)
    )
    on_current: set[int] = set()
    stale: list[tuple[int, str]] = []
    for profile in listed.profiles:
        url, appli = profile.callbackurl.get_secret_value(), int(profile.appli)
        if not _under_base(url, callback_base):
            continue
        if url == current_url:
            on_current.add(appli)
        else:
            stale.append((appli, url))
    wanted = _wanted(account.scopes, applis)
    revoked = updated = subscribed = 0
    grouped: dict[int, list[str]] = {}
    for appli, url in stale:
        grouped.setdefault(appli, []).append(url)
    if resubscribe:
        for appli, urls in grouped.items():
            for url in urls:
                await _revoke(job, account, client, lane, budget, appli, url)
                revoked += 1
    else:
        for appli, urls in grouped.items():
            if appli in on_current or appli not in wanted:
                for url in urls:
                    await _revoke(job, account, client, lane, budget, appli, url)
                    revoked += 1
                continue
            first, *rest = urls
            await _notify(
                job,
                account,
                lane,
                lambda appli=appli, first=first: client.notify_update(
                    account.access_token,
                    callbackurl=first,
                    appli=appli,
                    new_callbackurl=current_url,
                    budget=budget,
                ),
            )
            updated += 1
            for url in rest:
                await _revoke(job, account, client, lane, budget, appli, url)
                revoked += 1
    for appli in wanted:
        if appli in on_current or (not resubscribe and appli in grouped):
            continue
        await _notify(
            job,
            account,
            lane,
            lambda appli=appli: client.notify_subscribe(
                account.access_token, callbackurl=current_url, appli=appli, budget=budget
            ),
        )
        subscribed += 1
    logger.info(
        "withings subscriptions repaired",
        extra={
            "connection_id": str(account.connection_id), "revoked": revoked,
            "updated": updated, "subscribed": subscribed, "resubscribe": resubscribe,
        },
    )  # fmt: skip
    return RepairResult(revoked, updated, subscribed)


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
