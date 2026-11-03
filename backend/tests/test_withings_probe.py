"""The probe's shape-only report (#29, PLAN-operations §10.4) against the fake, no database.

Every value here is synthetic. The dataset is the fake's generator plus planted records whose
values, timestamps, ids and text are distinctive, so a leak into either output format is found
by a plain substring search.
"""

import argparse
import io
import json
import re
import urllib.request
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from ownvitals import cli, egress
from ownvitals.config import Settings
from ownvitals.egress import core
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.errors import RetryBudget
from ownvitals.withings.probe import (
    EP_GOALS,
    EP_HEART_GET,
    EP_HEART_LIST,
    EP_MEASURES,
    HEADER,
    SKIPPED_SCOPE,
    Report,
    as_dict,
    collect,
    render_json,
    render_text,
)
from ownvitals.withings.probe import run as probe_run
from withingscore import FakeWithings
from withingsdata import SyntheticDataset, generate_dataset
from withingsfake import FakeWithingsTransport
from withingsstreams import START
from withingswire import Subscription

CLIENT = "synthetic-client"
SECRET = "synthetic-secret"
CONNECTION = "5e7a1c2d-0000-4000-8000-00000000c0de"
NOW = datetime.fromtimestamp(START, UTC)

# Planted, distinctive and synthetic: none of these may appear in either output.
PLANTED_TIMESTAMP = START - 30
PLANTED_GROUP: dict[str, Any] = {
    "grpid": 4_242_424,
    "attrib": 0,
    "date": 1_771_234_567,
    "created": 1_771_234_568,
    "modified": 1_771_234_569,
    "category": 1,
    "deviceid": "synthetic-dev-c0ffee42",
    "hash_deviceid": "synthetic-hash-badf00d9",
    "model": "Synthetic Scale Qx",
    "model_id": 6,
    "timezone": "Synthetic/Zone_Qx",
    "comment": "synthetic-comment-zq",
    "measures": [
        {"value": 987_654_321, "type": 1, "unit": -7, "algo": 0, "fm": 131, "position": 13},
        {"value": 123_456_789, "type": 9_876, "unit": -2},
    ],
    "lat_extra": 59.123456,
    "1771234570": 5,
    "Synthetic Note": "synthetic-free-text-qq",
}
PLANTED_SAMPLES = [31_337, -27_183, 16_180, -14_142, 17_320] * 600
PLANTED_ECG: dict[str, Any] = {
    "list": {
        "deviceid": "synthetic-dev-ecg-77aa",
        "model": 93,
        "ecg": {"signalid": 8_888_888, "afib": 2, "afib_details": 13},
        "heart_rate": 173,
        "modified": PLANTED_TIMESTAMP + 7,
        "timestamp": PLANTED_TIMESTAMP,
        "timezone": "Synthetic/Zone_Qx",
    },
    "signal": {
        "signal": PLANTED_SAMPLES,
        "sampling_frequency": 512,
        "wearposition": 3,
        "model": 93,
        "heart_rate": {
            "grpid": 0,
            "value": 173,
            "date": PLANTED_TIMESTAMP,
            "is_deleted": False,
        },
        "signal_lead2": [29_979, -24_601] * 1_000,
    },
}
PLANTED_URL = "https://synthetic.example.test/api/webhooks/withings/s3cr3t-synthetic-hook-abc"
PLANTED_COMMENT = "synthetic-sub-comment-zz"
NEEDLES = (
    "987654321",
    "123456789",
    "4242424",
    "1771234567",
    "1771234568",
    "1771234569",
    "1771234570",
    str(PLANTED_TIMESTAMP),
    str(PLANTED_TIMESTAMP + 7),
    "synthetic",  # every planted string carries it
    "c0ffee42",
    "badf00d9",
    "Zone_Qx",
    "59.123456",
    "8888888",
    "31337",
    "27183",
    "16180",
    "14142",
    "17320",
    "29979",
    "24601",
    "s3cr3t",
    "Synthetic Note",
)


@pytest.fixture(autouse=True)
def _worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    egress.install_worker_allowlist(ai_enabled=False)


class Unlimited:
    """A limiter that always grants; it counts the slots and records each pause."""

    def __init__(self) -> None:
        self.slots = 0
        self.open = 0
        self.paused: list[float] = []

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        self.slots += 1
        self.open += 1
        try:
            yield
        finally:
            self.open -= 1

    async def pause(self, wait_seconds: float) -> None:
        self.paused.append(wait_seconds)


class Held:
    """The access token, and what one renewal gives (None: the grant is gone)."""

    def __init__(self, access: str, fresh: str | None = None) -> None:
        self.access = access
        self.state = "stored"
        self.fresh = fresh
        self.renewed = 0

    async def renew(self) -> bool:
        self.renewed += 1
        if self.fresh is None:
            self.state = "needs_reconnect"
            return False
        self.access, self.state = self.fresh, "refreshed"
        return True


def _dataset() -> SyntheticDataset:
    ds = generate_dataset(29, days=20, quirks=True, ecg_count=4)
    ds.measuregrps.append(dict(PLANTED_GROUP))
    ds.ecg.append(PLANTED_ECG)
    return ds


@pytest.fixture
def fake() -> FakeWithings:
    fake = FakeWithings(seed=29)
    fake.add_app(CLIENT, SECRET)
    return fake


def _user(fake: FakeWithings, scope: tuple[str, ...] | None = None) -> tuple[int, str]:
    user = fake.add_user(dataset=_dataset())
    fake.subscriptions.append(Subscription(CLIENT, user.userid, 1, PLANTED_URL, PLANTED_COMMENT))
    fake.subscriptions.append(Subscription(CLIENT, user.userid, 54, PLANTED_URL, PLANTED_COMMENT))
    body = fake.tokens_for(CLIENT, user.userid, *(() if scope is None else (scope,)))
    return user.userid, str(body["access_token"])


async def _collect(
    fake: FakeWithings,
    tokens: Held,
    scopes: tuple[str, ...] = ("user.info", "user.metrics", "user.activity"),
    *,
    limiter: Unlimited | None = None,
    budget: RetryBudget | None = None,
) -> Report:
    async with WithingsClient.open(
        WithingsApp(CLIENT, SECRET), inner=FakeWithingsTransport(fake)
    ) as client:
        return await collect(
            client,
            tokens,
            limiter or Unlimited(),
            scopes,
            UUID(CONNECTION),
            now=NOW,
            budget=budget,
        )


def _leaves(value: Any) -> Iterator[Any]:
    """Every value (not the keys: field names may be printed)."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _dataset_needles(ds: SyntheticDataset) -> set[str]:
    """Every timestamp, id, large value, decimal and text value in the dataset."""
    found: set[str] = set()
    for leaf in _leaves(ds.to_dict()):
        if isinstance(leaf, bool):
            continue
        if isinstance(leaf, int) and abs(leaf) >= 100_000:
            found.add(str(leaf))
        elif isinstance(leaf, float | Decimal):
            found.add(repr(leaf) if isinstance(leaf, float) else str(leaf))
        elif isinstance(leaf, str) and len(leaf) >= 6:
            found.add(leaf)  # dates, zones, device ids, model names, comments
    return found


async def test_no_distinctive_value_timestamp_or_id_reaches_either_format(
    fake: FakeWithings,
) -> None:
    userid, access = _user(fake)
    report = await _collect(fake, Held(access))
    text, as_json = render_text(report), render_json(report)
    assert report.stopped is None
    ds = fake.users[userid].data
    needles = set(NEEDLES) | _dataset_needles(ds) | {access, PLANTED_URL, PLANTED_COMMENT}
    needles |= {str(fake.grant(CLIENT, userid).refresh), str(userid)}
    for output in (text, as_json):
        leaked = sorted(n for n in needles if n in output)
        assert leaked == [], leaked
        assert not re.search(r"\d\.\d", output)  # no decimal value of any kind


async def test_the_json_holds_only_counts_codes_and_names(fake: FakeWithings) -> None:
    _, access = _user(fake)
    data = json.loads(render_json(await _collect(fake, Held(access))))
    assert data["connection"] == CONNECTION
    numbers = [
        leaf for leaf in _leaves(data) if isinstance(leaf, int) and not isinstance(leaf, bool)
    ]
    assert numbers
    assert max(numbers) < 100_000  # counts, lengths and codes; never a timestamp
    for leaf in [*_leaves(data), *_keys(data)]:
        if isinstance(leaf, str) and leaf != CONNECTION:
            assert re.fullmatch(r"[a-z0-9_:/ ()-]*", leaf), leaf


async def test_the_shapes_match_the_dataset(fake: FakeWithings) -> None:
    userid, access = _user(fake)
    limiter = Unlimited()
    report = await _collect(fake, Held(access), limiter=limiter)
    ds = fake.users[userid].data
    measures = [measure for group in ds.measuregrps for measure in group["measures"]]
    assert report.measures.groups == len(ds.measuregrps)
    assert report.measures.meastypes == Counter(measure["type"] for measure in measures)
    assert report.measures.attrib == Counter(g.get("attrib", "absent") for g in ds.measuregrps)
    assert report.measures.position == Counter(m.get("position", "absent") for m in measures)
    assert report.measures.position[13] == 1
    assert "other" in report.measures.group_fields  # the numeric and the spaced keys
    assert {"lat_extra", "deviceid", "timezone"} <= report.measures.group_fields
    ecg = report.ecg
    assert ecg.listed == len(ds.ecg)
    assert ecg.fetched == 3
    assert ecg.arrays[2] == 1  # the planted recording sends a second array
    assert ecg.lengths[len(PLANTED_SAMPLES)] == 1
    assert ecg.lengths[2_000] == 1
    assert ecg.sampling_rate[512] == 1
    assert ecg.wearposition[3] >= 1
    assert "signal_lead2" in ecg.signal_fields
    assert report.notify.subscriptions == 2
    assert report.notify.appli == Counter({1: 1, 54: 1})
    assert report.notify.fields == {"appli", "callbackurl", "comment"}
    assert report.capabilities == {
        "activity_daily": "supported",
        "devices": "supported",
        "ecg": "supported",
        "goals": "supported",
        "intraday": "supported",
        "measures": "supported",
        "sleep_series": "supported",
        "sleep_summary": "supported",
        "workouts": "supported",
    }
    assert set(report.calls.values()) == {"ok"}
    assert limiter.slots == fake.request_count  # every call took a slot
    assert limiter.open == 0


async def test_the_text_lists_the_same_content_with_the_header(fake: FakeWithings) -> None:
    _, access = _user(fake)
    report = await _collect(fake, Held(access))
    lines = render_text(report).splitlines()
    assert lines[0] == HEADER
    assert lines[1:4] == [f"connection {CONNECTION}", "grant stored", "outcome complete"]
    assert f"call {EP_MEASURES} ok" in lines
    assert "capability ecg supported" in lines
    assert "ecg sampling_rate 512 count 1" in lines
    assert "notify appli 54 count 1" in lines
    data = as_dict(report)
    assert f"measures groups {data['measures']['groups']}" in lines


async def test_a_missing_scope_skips_the_stream_as_not_authorized(fake: FakeWithings) -> None:
    _, access = _user(fake, scope=("user.metrics",))
    report = await _collect(fake, Held(access), scopes=("user.metrics",))
    assert report.capabilities["activity_daily"] == "not_authorized"
    assert report.capabilities["devices"] == "not_authorized"
    assert report.calls["v2/measure:getactivity"] == SKIPPED_SCOPE
    assert report.capabilities["measures"] == "supported"
    assert not any(entry.endpoint == "v2/measure:getactivity" for entry in fake.log)


async def test_a_refused_token_is_renewed_once(fake: FakeWithings) -> None:
    _, access = _user(fake)
    tokens = Held("synthetic-expired-token", fresh=access)
    report = await _collect(fake, tokens)
    assert tokens.renewed == 1
    assert report.grant == "refreshed"
    assert report.stopped is None


async def test_a_grant_that_cannot_be_renewed_stops_the_probe(fake: FakeWithings) -> None:
    _user(fake)
    tokens = Held("synthetic-expired-token")
    report = await _collect(fake, tokens)
    assert tokens.renewed == 1
    assert report.stopped == "token_invalid"
    assert report.grant == "needs_reconnect"
    assert render_text(report).splitlines()[3] == "outcome stopped token_invalid"
    assert len(fake.log) == 1  # one refused call: nothing after the stop


async def test_a_rate_limit_pauses_the_bucket_and_stops(fake: FakeWithings) -> None:
    _, access = _user(fake)
    fake.inject("status", at=1, endpoint=EP_MEASURES, status=601, wait_seconds=42)
    limiter = Unlimited()
    report = await _collect(fake, Held(access), limiter=limiter)
    assert limiter.paused == [42]
    assert report.stopped == "rate_limited"
    assert report.calls[EP_MEASURES] == "rate_limited"
    assert EP_HEART_LIST not in report.calls


async def test_permanent_and_transient_answers_are_categories(fake: FakeWithings) -> None:
    _, access = _user(fake)
    fake.inject("status", at=1, endpoint=EP_HEART_LIST, status=2554)
    fake.inject("http", at=1, endpoint=EP_GOALS, http_status=503)
    report = await _collect(fake, Held(access), budget=RetryBudget(retries=0))
    assert report.capabilities["ecg"] == "unsupported"
    assert report.calls[EP_HEART_LIST] == "permanent not_implemented api_status 2554"
    assert EP_HEART_GET not in report.calls
    assert report.capabilities["goals"] == "unknown"
    assert report.calls[EP_GOALS] == "transient http_status"
    assert report.stopped is None


async def test_an_unknown_api_status_prints_its_number_only(fake: FakeWithings) -> None:
    _, access = _user(fake)
    fake.inject("status", at=1, endpoint=EP_MEASURES, status=4_321)
    report = await _collect(fake, Held(access))
    assert report.calls[EP_MEASURES] == "permanent unknown_status api_status 4321"
    assert report.capabilities["measures"] == "unknown"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ownvitals")
    cli.add_subcommands(parser)
    return parser


def test_the_cli_installs_the_probe_with_its_options() -> None:
    args = _parser().parse_args(["probe", "--connection", CONNECTION, "--json"])
    assert (args.command, args.connection, args.json, args.ecg_signals) == (
        "probe",
        CONNECTION,
        True,
        3,
    )
    assert cli.COMMANDS["probe"] is probe_run


@pytest.mark.parametrize("value", ["11", "-1", "three"])
def test_the_ecg_signal_count_is_bounded(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["probe", "--connection", CONNECTION, "--ecg-signals", value])
    capsys.readouterr()


def test_a_connection_that_is_not_a_uuid_is_refused_before_anything_runs() -> None:
    args = _parser().parse_args(["probe", "--connection", "not-a-uuid"])
    out, err = io.StringIO(), io.StringIO()
    assert cli.run_command(args, Settings(), out, err) == 1
    assert (out.getvalue(), err.getvalue()) == ("", "error: the connection id is not a uuid\n")
