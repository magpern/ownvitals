"""What the probe (``probe.py``) collects: every stream called once (measures and the ECG list
in pages), keeping only shapes in the report."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from ownvitals.withings import models as m
from ownvitals.withings.client import WithingsClient
from ownvitals.withings.errors import RetryBudget
from ownvitals.withings.probe_call import (
    _FAILED,
    Limiter,
    Tokens,
    _allowed,
    _Caller,
    _capability,
    _Classified,
    _pages,
)
from ownvitals.withings.probe_report import Report, _code, _int_arrays, _names
from ownvitals.withings.subscriptions import APPLIS

MAX_MEASURE_PAGES: Final = 20  # 2 000 groups at the API's page size of 100
MAX_ECG_PAGES: Final = 10
DEFAULT_ECG_SIGNALS: Final = 3


RECENT_DAYS: Final = 30  # the window of the activity, workout and sleep-summary capability calls


# The codes the report may print as sent; any other value is ``other``. The documented enums
# (``models.DOCUMENTED_*``, the public spec's position enum 0..31, the notify applis) and, for the
# numbers that have no documented list, a bound well below any id or timestamp.
_ATTRIB: Final = m.DOCUMENTED_ATTRIB
_CATEGORY: Final = m.DOCUMENTED_CATEGORY
_POSITION: Final = frozenset(range(32))
_APPLI: Final = frozenset(APPLIS)
MAX_MEASTYPE: Final = 9_999
MAX_WEARPOSITION: Final = 31
MAX_SAMPLING_RATE: Final = 9_999
MAX_LENGTH: Final = 99_999
MAX_ARRAYS: Final = 12


def _label(path: str, action: str) -> str:
    """An endpoint's key as the client's errors name it (``path:action``). Built, not written
    as one literal: the egress guard reads a ``name:attr`` string as an import target."""
    return f"{path}:{action}"


EP_DEVICES: Final = _label("v2/user", "getdevice")
EP_GOALS: Final = _label("v2/user", "getgoals")
EP_MEASURES: Final = _label("measure", "getmeas")
EP_HEART_LIST: Final = _label("v2/heart", "list")
EP_HEART_GET: Final = _label("v2/heart", "get")
EP_ACTIVITY: Final = _label("v2/measure", "getactivity")
EP_WORKOUTS: Final = _label("v2/measure", "getworkouts")
EP_SLEEP_SUMMARY: Final = _label("v2/sleep", "getsummary")
EP_SLEEP: Final = _label("v2/sleep", "get")
EP_INTRADAY: Final = _label("v2/measure", "getintradayactivity")
EP_NOTIFY: Final = _label("notify", "list")


# ---------------------------------------------------------------------------------- collect


async def _devices(call: _Caller, client: WithingsClient, scopes: tuple[str, ...]) -> bool:
    """The devices capability; True when the account is known to have no device."""
    report = call.report
    if not _allowed(report, "devices", EP_DEVICES, scopes):
        return False
    got = await call(EP_DEVICES, lambda token: client.getdevice(token, budget=call.budget))
    empty = isinstance(got, m.DevicesBody) and not got.devices
    report.capabilities["devices"] = _capability("devices", got, empty=empty, devices_empty=False)
    return empty


async def _goals(call: _Caller, client: WithingsClient, scopes: tuple[str, ...]) -> None:
    if not _allowed(call.report, "goals", EP_GOALS, scopes):
        return
    got = await call(EP_GOALS, lambda token: client.getgoals(token, budget=call.budget))
    call.report.capabilities["goals"] = _capability("goals", got, empty=False, devices_empty=False)


async def _measures(call: _Caller, client: WithingsClient, scopes: tuple[str, ...]) -> None:
    report = call.report
    if not _allowed(report, "measures", EP_MEASURES, scopes):
        return
    pages, failed, truncated = await _pages(
        call,
        EP_MEASURES,
        lambda token, offset: client.getmeas(token, offset=offset, budget=call.budget),
        MAX_MEASURE_PAGES,
    )
    shape = report.measures
    shape.pages, shape.truncated = len(pages), truncated
    for page in pages:
        for group in page.measuregrps:
            shape.groups += 1
            shape.attrib[_code(group.attrib, known=_ATTRIB)] += 1
            shape.category[_code(group.category, known=_CATEGORY)] += 1
            shape.group_fields |= _names(group)
            for measure in group.measures:
                shape.meastypes[_code(measure.type, at_most=MAX_MEASTYPE)] += 1
                shape.position[_code(measure.position, known=_POSITION)] += 1
                shape.measure_fields |= _names(measure)
    got = pages if pages else failed
    report.capabilities["measures"] = _capability(
        "measures", got, empty=shape.groups == 0, devices_empty=False
    )


async def _ecg(
    call: _Caller,
    client: WithingsClient,
    scopes: tuple[str, ...],
    *,
    devices_empty: bool,
    signals: int,
) -> None:
    report = call.report
    if not _allowed(report, "ecg", EP_HEART_LIST, scopes):
        return
    pages, failed, truncated = await _pages(
        call,
        EP_HEART_LIST,
        lambda token, offset: client.heart_list(token, offset=offset, budget=call.budget),
        MAX_ECG_PAGES,
    )
    shape = report.ecg
    shape.pages, shape.truncated = len(pages), truncated
    recordings: list[m.HeartListEntry] = []
    for page in pages:
        for entry in page.series:
            shape.list_fields |= _names(entry)
            if entry.ecg is not None:
                recordings.append(entry)
    shape.listed = len(recordings)
    got = pages if pages else failed
    report.capabilities["ecg"] = _capability(
        "ecg", got, empty=shape.listed == 0, devices_empty=devices_empty
    )
    # The newest few; the timestamps only order them and are never kept.
    newest = sorted(recordings, key=lambda entry: entry.timestamp, reverse=True)[:signals]
    for entry in newest:
        signalid = entry.ecg.signalid if entry.ecg is not None else None
        if signalid is None:
            continue
        body = await call(
            EP_HEART_GET,
            lambda token, sid=signalid: client.heart_get(token, sid, budget=call.budget),
        )
        if not isinstance(body, m.HeartSignal):
            continue
        shape.fetched += 1
        lengths = [len(body.signal), *_int_arrays(body.raw)]
        shape.arrays[_code(len(lengths), at_most=MAX_ARRAYS)] += 1
        for length in lengths:
            shape.lengths[_code(length, at_most=MAX_LENGTH)] += 1
        shape.sampling_rate[_code(body.sampling_frequency, at_most=MAX_SAMPLING_RATE)] += 1
        shape.wearposition[_code(body.wearposition, at_most=MAX_WEARPOSITION)] += 1
        shape.signal_fields |= _names(body)


async def _recent(
    call: _Caller, client: WithingsClient, scopes: tuple[str, ...], now: datetime
) -> None:
    """One call per remaining stream, for its capability only: nothing of the answer is kept."""
    budget = call.budget
    end = now.date()
    start = end - timedelta(days=RECENT_DAYS)
    late = int(now.timestamp())
    early = late - 86_399  # the API's 24-hour limit for sleep series and intraday
    plan: tuple[tuple[str, str, Callable[[str], Awaitable[Any]], Callable[[Any], bool]], ...] = (
        (
            "activity_daily",
            EP_ACTIVITY,
            lambda t: client.getactivity(t, startdateymd=start, enddateymd=end, budget=budget),
            lambda b: not b.activities,
        ),
        (
            "workouts",
            EP_WORKOUTS,
            lambda t: client.getworkouts(t, startdateymd=start, enddateymd=end, budget=budget),
            lambda b: not b.series,
        ),
        (
            "sleep_summary",
            EP_SLEEP_SUMMARY,
            lambda t: client.sleep_getsummary(t, startdateymd=start, enddateymd=end, budget=budget),
            lambda b: not b.series,
        ),
        (
            "sleep_series",
            EP_SLEEP,
            lambda t: client.sleep_get(t, startdate=early, enddate=late, budget=budget),
            lambda b: not b.series,
        ),
        (
            "intraday",
            EP_INTRADAY,
            lambda t: client.getintradayactivity(t, startdate=early, enddate=late, budget=budget),
            lambda b: not b.series,
        ),
    )
    for stream, label, fetch, is_empty in plan:
        if not _allowed(call.report, stream, label, scopes):
            continue
        got = await call(label, fetch)
        empty = got is not _FAILED and not isinstance(got, _Classified) and is_empty(got)
        call.report.capabilities[stream] = _capability(
            stream, got, empty=empty, devices_empty=False
        )


async def _notify(call: _Caller, client: WithingsClient) -> None:
    got = await call(EP_NOTIFY, lambda token: client.notify_list(token, budget=call.budget))
    if not isinstance(got, m.NotifyListBody):
        return
    shape = call.report.notify
    for profile in got.profiles:
        shape.subscriptions += 1
        shape.appli[_code(profile.appli, known=_APPLI)] += 1
        shape.fields |= _names(profile)


async def collect(
    client: WithingsClient,
    tokens: Tokens,
    limiter: Limiter,
    scopes: tuple[str, ...],
    connection: UUID,
    *,
    now: datetime | None = None,
    ecg_signals: int = DEFAULT_ECG_SIGNALS,
    budget: RetryBudget | None = None,
) -> Report:
    """Call every stream once (measures and the ECG list in pages) and keep only shapes."""
    report = Report(connection)
    call = _Caller(report, tokens, limiter, budget or RetryBudget(retries=2))
    moment = now or datetime.now(UTC)
    devices_empty = await _devices(call, client, scopes)
    await _goals(call, client, scopes)
    await _measures(call, client, scopes)
    await _ecg(call, client, scopes, devices_empty=devices_empty, signals=ecg_signals)
    await _recent(call, client, scopes, moment)
    await _notify(call, client)
    report.grant = tokens.state
    return report
