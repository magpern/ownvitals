"""``probe --connection <id>``: the owner's shape-only check of a real Withings account
(PLAN-operations section 10.4, issue #29). Security-critical: it reads health data.

The owner runs it on the home server (``docs/runbooks/m2-probe.md``); agents never run it
against a real account. It calls the public API with the connection's stored grant, the way the
worker does, and prints **shapes only**:

- the outcome of every call (``ok``, a capability, or an error category with its API status);
- the capability state of every stream (``capabilities.py``), learned from the scopes and from
  these answers;
- measures: meastypes present and their counts, ``attrib``, ``category`` and ``position`` values
  with counts, and the names of the fields the groups and measures carry;
- ECG: how many recordings are listed, and for the newest few the number of arrays and their
  lengths, the sampling rate and the wear position, with counts, and the field names;
- notifications: the subscriptions' ``appli`` values and field names.

**Never** a measurement value, a sample, a timestamp, a date, a time zone, a device or user id, a
callback URL, a comment, a token or the provider's text: the report types hold only counts,
small codes the list above names, field names (filtered to plain identifiers) and categories
from fixed sets, so nothing else can reach the output (``tests/test_withings_probe*.py``).

Process role: this module is the one besides the worker's entry point that may install the
worker allowlist (``tests/egress_scan.py``, ``WORKER_ROLE_MODULES``); it never enables the model
host. Every call takes a token from the app's rate limiter on the user lane (``rate.py``) and
frees the slot after. A refused access token is refreshed once through the crash-safe path
(``tokens.py``), the only write the probe makes. The owner of the connection is found by visiting
each account's own session (``owner_context``, the way ``keys rewrap`` does), so the probe opens
no session itself. A failure prints the exception type only, never its message.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, TextIO
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ownvitals.config import Settings
from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.providers.app_secret import open_client_secret
from ownvitals.connections.providers.withings import open_withings
from ownvitals.connections.repository import connection_refs
from ownvitals.context import UserContext
from ownvitals.crypto.keks import KeyRingError, load_keyring
from ownvitals.db import (
    DatabaseRoleError,
    create_engine,
    create_session_factory,
    verify_database_role,
)
from ownvitals.egress import install_worker_allowlist
from ownvitals.jobs.issuer import owner_context
from ownvitals.withings import models as m
from ownvitals.withings import rate
from ownvitals.withings.capabilities import (
    NOT_AUTHORIZED,
    UNKNOWN,
    after_success,
    from_permanent,
    from_scopes,
)
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.errors import (
    PAGINATION,
    Permanent,
    RateLimited,
    RetryBudget,
    TokenInvalid,
    Transient,
)
from ownvitals.withings.streams import stream_named
from ownvitals.withings.tokens import recover_refresh, refresh_tokens

if TYPE_CHECKING:
    import httpx

Factory = async_sessionmaker[AsyncSession]

HEADER: Final = "# ownvitals probe: shapes only (no values, timestamps or ids but the connection)"
FORMAT: Final = "ownvitals-probe-1"
MAX_MEASURE_PAGES: Final = 20  # 2 000 groups at the API's page size of 100
MAX_ECG_PAGES: Final = 10
DEFAULT_ECG_SIGNALS: Final = 3
MAX_ECG_SIGNALS: Final = 10
RECENT_DAYS: Final = 30  # the window of the activity, workout and sleep-summary capability calls
MAX_RATE_WAIT: Final = 120.0  # seconds the probe waits for the limiter in all, then stops
ABSENT: Final = "absent"
# A field name the report may print: a plain lower-case identifier. Anything else (a key that is
# a number, a date or free text) is only counted as ``other``.
_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
_OTHER: Final = "other"


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
SKIPPED_SCOPE: Final = "skipped (no scope)"

Code = int | str  # an integer code from the API, or ``absent``


# ------------------------------------------------------------------------------------ report


@dataclass
class MeasureShape:
    groups: int = 0
    pages: int = 0
    truncated: bool = False
    meastypes: Counter[Code] = field(default_factory=Counter)
    attrib: Counter[Code] = field(default_factory=Counter)
    category: Counter[Code] = field(default_factory=Counter)
    position: Counter[Code] = field(default_factory=Counter)
    group_fields: set[str] = field(default_factory=set)
    measure_fields: set[str] = field(default_factory=set)


@dataclass
class EcgShape:
    listed: int = 0
    pages: int = 0
    truncated: bool = False
    fetched: int = 0
    arrays: Counter[Code] = field(default_factory=Counter)  # arrays per recording
    lengths: Counter[Code] = field(default_factory=Counter)  # samples per array
    sampling_rate: Counter[Code] = field(default_factory=Counter)
    wearposition: Counter[Code] = field(default_factory=Counter)
    list_fields: set[str] = field(default_factory=set)
    signal_fields: set[str] = field(default_factory=set)


@dataclass
class NotifyShape:
    subscriptions: int = 0
    appli: Counter[Code] = field(default_factory=Counter)
    fields: set[str] = field(default_factory=set)


@dataclass
class Report:
    """Everything the probe prints. Only counts, codes, field names and fixed categories."""

    connection: UUID
    grant: str = "stored"  # how the access token was obtained (a refresh outcome)
    stopped: str | None = None
    calls: dict[str, str] = field(default_factory=dict)
    capabilities: dict[str, str] = field(default_factory=dict)
    measures: MeasureShape = field(default_factory=MeasureShape)
    ecg: EcgShape = field(default_factory=EcgShape)
    notify: NotifyShape = field(default_factory=NotifyShape)

    @property
    def outcome(self) -> str:
        return "complete" if self.stopped is None else f"stopped {self.stopped}"


def _code(value: object) -> Code:
    """An integer code as sent, or ``absent``. Anything that is not an integer is ``other``."""
    if value is None:
        return ABSENT
    if isinstance(value, bool) or not isinstance(value, int):
        return _OTHER
    return value


def _names(model: BaseModel) -> set[str]:
    """The names of the fields ``model`` carries: known ones that are set, and unknown ones."""
    found = {
        name
        for name in type(model).model_fields
        if name != "raw" and getattr(model, name) is not None
    }
    raw = getattr(model, "raw", {})
    for key in raw:
        found.add(key if isinstance(key, str) and _NAME.fullmatch(key) else _OTHER)
    return found


def _int_arrays(raw: dict[str, Any]) -> list[int]:
    """Lengths of unknown fields that are arrays of integers (another lead, if one is sent)."""
    return [
        len(value)
        for value in raw.values()
        if isinstance(value, list)
        and value
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ]


# ----------------------------------------------------------------------------------- calling


class Stopped(Exception):
    """The probe stops: ``args == (reason,)``, a fixed category."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Limiter(Protocol):
    """One rate-limiter slot per call; ``pause`` is the 601 hold."""

    def slot(self) -> Any: ...  # an async context manager

    async def pause(self, wait_seconds: float) -> None: ...


class Tokens(Protocol):
    access: str
    state: str

    async def renew(self) -> bool:
        """A new access token after a refusal, once. False: the probe stops."""
        ...


@dataclass(frozen=True, slots=True)
class _Classified:
    capability: str


_FAILED: Final = object()


def _permanent(error: Permanent) -> str:
    parts = ["permanent", error.category]
    if error.api_status is not None:
        parts.append(f"api_status {int(error.api_status)}")
    if error.http_status is not None and error.http_status != 200:
        parts.append(f"http {int(error.http_status)}")
    return " ".join(parts)


class _Caller:
    def __init__(
        self, report: Report, tokens: Tokens, limiter: Limiter, budget: RetryBudget
    ) -> None:
        self.report = report
        self.tokens = tokens
        self.limiter = limiter
        self.budget = budget
        self.renewed = False  # one refresh for the whole probe

    def _stop(self, label: str, reason: str) -> object:
        self.report.calls[label] = reason
        self.report.stopped = reason
        return _FAILED

    async def __call__(self, label: str, fn: Callable[[str], Awaitable[Any]]) -> Any:
        """The answer, a ``_Classified`` capability, or ``_FAILED`` (recorded in the report)."""
        if self.report.stopped is not None:
            return _FAILED
        while True:
            try:
                async with self.limiter.slot():
                    result = await fn(self.tokens.access)
            except TokenInvalid:
                if not self.renewed:
                    self.renewed = True
                    if await self.tokens.renew():
                        continue
                self.report.grant = self.tokens.state
                return self._stop(label, "token_invalid")
            except RateLimited as exc:
                await self.limiter.pause(exc.wait_seconds)
                return self._stop(label, "rate_limited")
            except Stopped as exc:
                return self._stop(label, exc.reason)
            except Transient as exc:
                self.report.calls[label] = f"transient {exc.category}"
                return _FAILED
            except Permanent as exc:
                self.report.calls[label] = _permanent(exc)
                found = from_permanent(exc)
                return _FAILED if found is None else _Classified(found)
            self.report.grant = self.tokens.state
            self.report.calls.setdefault(label, "ok")
            return result


async def _pages(
    call: _Caller, label: str, fetch: Callable[[str, int | None], Awaitable[Any]], cap: int
) -> tuple[list[Any], Any, bool]:
    """Offset pages up to ``cap``: ``(pages, first failure or None, truncated)``."""
    pages: list[Any] = []
    offset: int | None = None
    for _ in range(cap):
        page = await call(label, lambda token, at=offset: fetch(token, at))
        if page is _FAILED or isinstance(page, _Classified):
            return pages, page, False
        pages.append(page)
        if not page.more:
            return pages, None, False
        if page.offset <= (offset or 0):
            call.report.calls[label] = f"permanent {PAGINATION}"
            return pages, None, False
        offset = page.offset
    return pages, None, True


def _capability(stream: str, got: Any, *, empty: bool, devices_empty: bool) -> str:
    if isinstance(got, _Classified):
        return got.capability
    if got is _FAILED:
        return UNKNOWN
    return after_success(stream=stream, empty=empty, devices_known_empty=devices_empty)


def _allowed(report: Report, stream: str, label: str, scopes: tuple[str, ...]) -> bool:
    if from_scopes(stream_named(stream), scopes) == NOT_AUTHORIZED:
        report.capabilities[stream] = NOT_AUTHORIZED
        report.calls.setdefault(label, SKIPPED_SCOPE)
        return False
    return True


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
            shape.attrib[_code(group.attrib)] += 1
            shape.category[_code(group.category)] += 1
            shape.group_fields |= _names(group)
            for measure in group.measures:
                shape.meastypes[_code(measure.type)] += 1
                shape.position[_code(measure.position)] += 1
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
        shape.arrays[len(lengths)] += 1
        for length in lengths:
            shape.lengths[length] += 1
        shape.sampling_rate[_code(body.sampling_frequency)] += 1
        shape.wearposition[_code(body.wearposition)] += 1
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
        shape.appli[_code(profile.appli)] += 1
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


# ---------------------------------------------------------------------------------- output


def _sorted_counts(counter: Counter[Code]) -> list[tuple[Code, int]]:
    ints = sorted((k, v) for k, v in counter.items() if isinstance(k, int))
    names = sorted((k, v) for k, v in counter.items() if isinstance(k, str))
    return [*ints, *names]


def as_dict(report: Report) -> dict[str, Any]:
    """The report as JSON-ready data (``--json``); the same content as the text."""

    def counts(counter: Counter[Code]) -> dict[str, int]:
        return {str(key): int(total) for key, total in _sorted_counts(counter)}

    ms, ecg, notify = report.measures, report.ecg, report.notify
    return {
        "format": FORMAT,
        "connection": str(report.connection),
        "grant": report.grant,
        "outcome": report.outcome,
        "calls": dict(sorted(report.calls.items())),
        "capabilities": dict(sorted(report.capabilities.items())),
        "measures": {
            "groups": ms.groups,
            "pages": ms.pages,
            "truncated": ms.truncated,
            "meastypes": counts(ms.meastypes),
            "attrib": counts(ms.attrib),
            "category": counts(ms.category),
            "position": counts(ms.position),
            "group_fields": sorted(ms.group_fields),
            "measure_fields": sorted(ms.measure_fields),
        },
        "ecg": {
            "listed": ecg.listed,
            "pages": ecg.pages,
            "truncated": ecg.truncated,
            "fetched": ecg.fetched,
            "arrays": counts(ecg.arrays),
            "lengths": counts(ecg.lengths),
            "sampling_rate": counts(ecg.sampling_rate),
            "wearposition": counts(ecg.wearposition),
            "list_fields": sorted(ecg.list_fields),
            "signal_fields": sorted(ecg.signal_fields),
        },
        "notify": {
            "subscriptions": notify.subscriptions,
            "appli": counts(notify.appli),
            "fields": sorted(notify.fields),
        },
    }


def render_json(report: Report) -> str:
    return json.dumps(as_dict(report), indent=2, sort_keys=False)


def render_text(report: Report) -> str:
    """Plain lines the owner pastes into the issue."""
    data = as_dict(report)
    lines = [
        HEADER,
        f"connection {data['connection']}",
        f"grant {data['grant']}",
        f"outcome {data['outcome']}",
    ]
    lines += [f"call {label} {result}" for label, result in data["calls"].items()]
    lines += [f"capability {stream} {state}" for stream, state in data["capabilities"].items()]
    for section in ("measures", "ecg", "notify"):
        for key, value in data[section].items():
            if isinstance(value, bool):
                lines.append(f"{section} {key} {'yes' if value else 'no'}")
            elif isinstance(value, int):
                lines.append(f"{section} {key} {value}")
            elif isinstance(value, list):
                lines.append(f"{section} {key} {','.join(value) if value else 'none'}")
            elif not value:
                lines.append(f"{section} {key} none")
            else:
                lines += [f"{section} {key} {code} count {n}" for code, n in value.items()]
    return "\n".join(lines)


# ----------------------------------------------------------------------- database and CLI


class _Bucket:
    """The app's rate limiter (``rate.py``) on the user lane: a token before every call."""

    def __init__(
        self,
        engine: AsyncEngine,
        app_id: int,
        connection_id: UUID,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_wait: float = MAX_RATE_WAIT,
    ) -> None:
        self._engine = engine
        self._app = app_id
        self._connection = connection_id
        self._sleep = sleep
        self._left = max_wait

    async def _claim(self) -> rate.Claim:
        while True:
            async with self._engine.begin() as conn:
                claim = await conn.run_sync(
                    lambda sync: rate.claim(sync, self._app, self._connection, rate.LANE_SYNC_NOW)
                )
            if claim.granted:
                return claim
            wait = 5.0
            if claim.retry_at is not None:
                wait = (claim.retry_at - datetime.now(UTC)).total_seconds()
            wait = min(max(wait, 1.0), 60.0)
            if wait > self._left:
                raise Stopped("rate_limited")
            self._left -= wait
            await self._sleep(wait)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        claim = await self._claim()
        try:
            yield
        finally:
            if claim.lease_id is not None:
                lease = claim.lease_id
                async with self._engine.begin() as conn:
                    await conn.run_sync(lambda sync: rate.release(sync, lease))

    async def pause(self, wait_seconds: float) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(lambda sync: rate.pause(sync, self._app, wait_seconds))


class _StoredTokens:
    """The connection's access token; one refresh through ``tokens.py`` after a refusal."""

    def __init__(self, access: str, renew: Callable[[], Awaitable[tuple[str, str]]]) -> None:
        self.access = access
        self.state = "stored"
        self._renew = renew

    async def renew(self) -> bool:
        outcome, access = await self._renew()
        self.state = outcome
        if access:
            self.access = access
            self.state = "refreshed" if outcome == "refreshed" else "current"
            return True
        return False


def _scopes(public: dict[str, Any]) -> tuple[str, ...]:
    raw = public.get("scopes") or ()
    if isinstance(raw, str):
        raw = [part for part in raw.split(",") if part]
    return tuple(str(item) for item in raw) if isinstance(raw, list | tuple) else ()


async def _owner(factory: Factory, connection_id: UUID) -> UserContext | None:
    """The owner's bare context, found in each account's own session; None when no account has
    a live Withings connection with this id."""
    # The account list is the operator CLI's (``keys rewrap`` visits accounts the same way).
    # Imported here: ``ownvitals.auth.operator`` brings the web-only mail module, which no
    # module of this worker package may import at its top (``tests/egress_worker.py``).
    from ownvitals.auth.operator import user_ids

    for user_id in await user_ids(factory):
        ctx = owner_context(user_id)
        if ("withings", connection_id) in await connection_refs(factory, ctx):
            return ctx
    return None


async def probe(
    settings: Settings,
    connection_id: UUID,
    out: TextIO,
    err: TextIO,
    *,
    as_json: bool = False,
    ecg_signals: int = DEFAULT_ECG_SIGNALS,
    inner: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Open the grant, call Withings, print the shapes. 0 when complete, else 1. ``inner``
    replaces the network in tests (every egress check still runs)."""
    engine = create_engine(settings)
    try:
        await verify_database_role(engine)
        factory = create_session_factory(engine)
        ring = await load_keyring(factory, settings, role="worker")
        ctx = await _owner(factory, connection_id)
        if ctx is None:
            print("error: no live Withings connection has this id", file=err)
            return 1
        view = await open_withings(factory, ring, ctx, connection_id)
        app_id = int(view.public["app_id"])
        secret = await open_client_secret(factory, ring, ctx, app_id)
        app = WithingsApp(secret.client_id, secret.secret)
        async with WithingsClient.open(app, inner=inner) as client:

            async def renew() -> tuple[str, str]:
                done = await refresh_tokens(factory, ring, ctx, connection_id, client)
                if done.outcome == "recover":
                    done = await recover_refresh(factory, ring, ctx, connection_id, client)
                return done.outcome, done.access_token

            report = await collect(
                client,
                _StoredTokens(view.access_token, renew),
                _Bucket(engine, app_id, connection_id, sleep=sleep),
                _scopes(view.public),
                connection_id,
                ecg_signals=ecg_signals,
            )
        print(render_json(report) if as_json else render_text(report), file=out)
        return 0 if report.stopped is None else 1
    except ConnectionFailure as failure:  # a fixed category
        print(f"error: {failure.category}", file=err)
        return 1
    except (DatabaseRoleError, KeyRingError) as refused:  # names roles and keys, never a secret
        print(f"error: {type(refused).__name__}: {refused}", file=err)
        return 1
    except Exception as exc:
        print(f"error: {type(exc).__name__}", file=err)
        return 1
    finally:
        await engine.dispose()


def _signals(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("a whole number") from None
    if not 0 <= number <= MAX_ECG_SIGNALS:
        raise argparse.ArgumentTypeError(f"between 0 and {MAX_ECG_SIGNALS}")
    return number


def add_probe_subcommand(sub: Any) -> None:
    """``probe --connection <uuid> [--json] [--ecg-signals N]``."""
    parser = sub.add_parser(
        "probe", help="print the shapes of a real Withings account's data (no values)"
    )
    parser.add_argument("--connection", required=True, metavar="UUID")
    parser.add_argument("--json", action="store_true", help="the same content as JSON")
    parser.add_argument(
        "--ecg-signals",
        type=_signals,
        default=DEFAULT_ECG_SIGNALS,
        metavar="N",
        help=f"how many of the newest ECG recordings to fetch (0 to {MAX_ECG_SIGNALS})",
    )


async def run(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """The CLI command. Makes this process the worker first: the probe calls Withings."""
    try:
        connection_id = UUID(str(args.connection))
    except ValueError:
        print("error: the connection id is not a uuid", file=err)
        return 1
    install_worker_allowlist(ai_enabled=False)  # never the model host
    return await probe(
        settings,
        connection_id,
        out,
        err,
        as_json=bool(args.json),
        ecg_signals=int(args.ecg_signals),
    )
