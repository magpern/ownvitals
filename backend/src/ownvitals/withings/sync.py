"""The sync engine (PLAN section 4.4).

`withings_connect` reads devices, goals and capabilities, lists subscriptions and enqueues
one backfill per stream the grant allows. `withings_backfill` walks that stream's windows
from the cursor, so a lost lease resumes at the next window and a finished run ingests
nothing a second time. `withings_sync` is the incremental pass (the queue debounces it).
`ecg_fetch` lists recordings since the cursor and stores each signal. `withings_poll`
enqueues a backfill for a stream that has not finished and an incremental sync for one that has.

Every Withings call takes a rate-limiter token first and releases the slot after, including
when the answer is a capability. An unsupported stream is recorded as that state and no zero
row is written. Partition months are created one at a time and committed before the rows.

Handlers are not attached to the API's registry (``KINDS``, which stays empty).
`register_sync_kinds` (``sync_kinds.py``) registers the five kinds on the registry it is given;
the worker's registry (``ownvitals.worker.kinds``) is built with it. The connect and poll passes
live in ``sync_connect.py``; this module keeps backfill, the incremental pass and the cursor.
"""

from datetime import UTC, datetime
from typing import Any

from ownvitals.ingest.calls import Account, Classified, Halt
from ownvitals.ingest.codec import payload_hash
from ownvitals.ingest.cursors import read_cursor, write_cursor
from ownvitals.ingest.fetch import ecg_window, measure_window, once, window
from ownvitals.ingest.fetch_incremental import incremental
from ownvitals.ingest.revisions import bump
from ownvitals.ingest.when import from_epoch
from ownvitals.jobs import JobFailure
from ownvitals.jobs.run import JobRun
from ownvitals.jobs.withings import lane_for
from ownvitals.withings.capabilities import NOT_AUTHORIZED, UNKNOWN, after_success, from_scopes
from ownvitals.withings.client import WithingsClient
from ownvitals.withings.errors import RetryBudget
from ownvitals.withings.streams import stream_named, windows


def _lane(kind: str) -> int:
    origin = "user" if kind == "withings_connect" else "scheduler"
    return lane_for(kind, origin)


async def _owner_cursor(job: JobRun, account: Account, name: str) -> dict[str, Any]:
    async with job.fenced() as fs:
        return await read_cursor(fs.db, job.ctx.user_id, account.connection_id, name)


async def _store(
    job: JobRun,
    account: Account,
    name: str,
    *,
    capability: str,
    state: dict[str, Any],
    lastupdate: int | None,
    zone: str | None,
    covered_from: datetime | None,
    covered_to: datetime | None,
    done: bool,
    bump_goals: bool = False,
) -> None:
    async with job.fenced() as fs:
        await write_cursor(
            fs.db,
            job.ctx.user_id,
            account.connection_id,
            name,
            capability=capability,
            state=state,
            lastupdate=lastupdate,
            covered_from=covered_from,
            covered_to=covered_to,
            last_ok=datetime.now(UTC) if done else None,
            zone=zone,
        )
        if bump_goals:
            await bump(fs.db, job.ctx.user_id, "goals")


async def execute_backfill(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    name: str,
    *,
    lane: int | None = None,
    halt: Halt | None = None,
    now: datetime | None = None,
) -> str:
    """Walk ``name`` until its cursor is complete. Returns the capability that was stored."""
    try:
        spec = stream_named(name)
    except KeyError:
        raise JobFailure("invalid_input") from None
    moment = now or datetime.now(UTC)
    lane = _lane("withings_backfill") if lane is None else lane
    cursor = await _owner_cursor(job, account, name)
    state = dict(cursor["state"])
    granted = from_scopes(spec, account.scopes)
    if granted == NOT_AUTHORIZED:
        state.update(status="complete", index=0, skipped_out_of_range=0)
        await _store(
            job,
            account,
            name,
            capability=NOT_AUTHORIZED,
            state=state,
            lastupdate=None,
            zone=account.zone,
            covered_from=None,
            covered_to=None,
            done=True,
        )
        return NOT_AUTHORIZED
    demand = state.get("demand_days")
    spans = windows(spec, moment, demand_days=demand if isinstance(demand, int) else None)
    index = int(state.get("index") or 0)
    if state.get("status") == "complete" and index >= len(spans):
        return str(cursor["capability"] or UNKNOWN)
    skipped = int(state.get("skipped_out_of_range") or 0)
    lastupdate = cursor["lastupdate"]
    zone = cursor["zone"] or account.zone
    empty = True
    budget = RetryBudget(retries=2)
    covered_from = from_epoch(spans[0][0]) if spans and spans[0] != (0, 0) else None
    while index < len(spans):
        await job.checkpoint()
        start, end = spans[index]
        got = await window(job, account, client, lane, budget, spec.shape, start, end, moment)
        if isinstance(got, Classified):
            state.update(status="complete", index=index, skipped_out_of_range=skipped)
            await _store(
                job,
                account,
                name,
                capability=got.capability,
                state=state,
                lastupdate=lastupdate,
                zone=zone,
                covered_from=covered_from,
                covered_to=None,
                done=True,
            )
            return got.capability
        skipped += got.skipped
        zone = got.zone or zone
        if got.lastupdate is not None:
            lastupdate = got.lastupdate
        if got.no_devices is not None:
            state["no_devices"] = got.no_devices
        goals_changed = False
        if got.goals is not None:
            digest = payload_hash(got.goals).hex()
            goals_changed = state.get("goals_hash") != digest
            state["goals"] = got.goals
            state["goals_hash"] = digest
        empty = empty and got.empty
        index += 1
        done = index >= len(spans)
        devices_empty = False
        if spec.shape == "ecg":
            devices = await _owner_cursor(job, account, "devices")
            devices_empty = devices["state"].get("no_devices") is True
        capability = after_success(
            stream=name, empty=empty and done, devices_known_empty=devices_empty
        )
        if not done:
            capability = str(cursor["capability"] or UNKNOWN) if empty else capability
        state.update(
            status="complete" if done else "running", index=index, skipped_out_of_range=skipped
        )
        covered_to = from_epoch(end) if spans[index - 1] != (0, 0) else None
        await _store(
            job,
            account,
            name,
            capability=capability if done or not empty else UNKNOWN,
            state=state,
            lastupdate=lastupdate,
            zone=zone,
            covered_from=covered_from,
            covered_to=covered_to,
            done=done,
            bump_goals=goals_changed,
        )
        await job.progress(index, max(len(spans), 1))
        if halt is not None:
            halt.seen += 1
            if halt.seen >= halt.after_windows:
                from ownvitals.jobs.errors import RunStopped

                raise RunStopped("lost")
    return str((await _owner_cursor(job, account, name))["capability"] or UNKNOWN)


async def execute_sync(
    job: JobRun,
    account: Account,
    client: WithingsClient,
    name: str,
    *,
    lane: int | None = None,
    now: datetime | None = None,
) -> str:
    """Incremental pass. A stream whose backfill is not complete is left incomplete.

    Activity, workouts and sleep summary call ``lastupdate`` and store the new cursor.
    Sleep series and intraday walk forward from the cursor, a day at a time, so a gap
    longer than 24 hours is still fetched. Backfill keeps its own windows.
    """
    try:
        spec = stream_named(name)
    except KeyError:
        raise JobFailure("invalid_input") from None
    cursor = await _owner_cursor(job, account, name)
    if cursor["state"].get("status") != "complete":
        return str(cursor["capability"] or from_scopes(spec, account.scopes))
    if from_scopes(spec, account.scopes) == NOT_AUTHORIZED:
        return NOT_AUTHORIZED
    moment = now or datetime.now(UTC)
    lane = _lane("withings_sync") if lane is None else lane
    budget = RetryBudget(retries=2)
    if spec.shape == "measures":
        if cursor["lastupdate"] is None:
            return str(cursor["capability"] or UNKNOWN)
        got = await measure_window(
            job, account, client, lane, budget, 0, 0, moment, lastupdate=int(cursor["lastupdate"])
        )
    elif spec.shape == "ecg":
        start = int(cursor["lastupdate"] or 0)
        got = await ecg_window(
            job, account, client, lane, budget, start, int(moment.timestamp()), moment
        )
    elif spec.shape in {"devices", "goals"}:
        got = await once(job, account, client, lane, budget, spec.shape, moment)
    else:
        mark = None if cursor["lastupdate"] is None else int(cursor["lastupdate"])
        got = await incremental(
            job,
            account,
            client,
            lane,
            budget,
            spec.shape,
            mark,
            int(moment.timestamp()),
            moment,
            history_days=spec.history_days,
        )
    if isinstance(got, Classified):
        state = dict(cursor["state"])
        state["status"] = "complete"
        await _store(
            job,
            account,
            name,
            capability=got.capability,
            state=state,
            lastupdate=cursor["lastupdate"],
            zone=cursor["zone"],
            covered_from=None,
            covered_to=None,
            done=True,
        )
        return got.capability
    if got.lastupdate is not None:
        state = dict(cursor["state"])
        await _store(
            job,
            account,
            name,
            capability=str(cursor["capability"] or UNKNOWN),
            state=state,
            lastupdate=got.lastupdate,
            zone=got.zone or cursor["zone"],
            covered_from=None,
            covered_to=None,
            done=True,
        )
    return str(cursor["capability"] or UNKNOWN)
