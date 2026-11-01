"""The Withings client's endpoints against the in-process fake (PLAN §4.2, #20): every endpoint,
paging, undocumented enum values, missing optional fields and partial records.

The client runs over ``egress.withings_async_client`` with the fake as its network transport, so
every egress check runs too (the worker allowlist is installed and reset per test). Synthetic data
only (``withingsdata``), no network.
"""

import urllib.request
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from ownvitals import egress
from ownvitals.egress import core
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.models import DOCUMENTED_ATTRIB, MeasureGroup
from withingscore import FakeWithings
from withingsdata import UNDOCUMENTED_ATTRIB, UNDOCUMENTED_MEASTYPE, unit_of
from withingsfake import FakeWithingsTransport
from withingsstreams import DAY, START, UNDOCUMENTED_SLEEP_STATE, ymd
from withingswire import App, FakeUser

HOOK = "https://app.example.test/api/webhooks/withings/synthetic-hook-secret"


@pytest.fixture(autouse=True)
def _worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    egress.install_worker_allowlist(ai_enabled=False)


@dataclass
class World:
    fake: FakeWithings
    app: App
    user: FakeUser
    client: WithingsClient
    token: str
    refresh_token: str


async def _world(*, quirks: bool = False, page_size: int = 100, **options: Any) -> World:
    fake = FakeWithings(seed=3, page_size=page_size)
    app = fake.add_app()
    user = fake.add_user(days=20, quirks=quirks, **options)
    tokens = fake.tokens_for(app.client_id, user.userid)
    return World(fake, app, user, None, tokens["access_token"], tokens["refresh_token"])  # type: ignore[arg-type]


@pytest.fixture
async def world() -> AsyncIterator[World]:
    w = await _world()
    async with WithingsClient.open(_app(w), inner=FakeWithingsTransport(w.fake)) as client:
        w.client = client
        yield w


@pytest.fixture
async def quirky() -> AsyncIterator[World]:
    w = await _world(quirks=True)
    async with WithingsClient.open(_app(w), inner=FakeWithingsTransport(w.fake)) as client:
        w.client = client
        yield w


def _app(w: World) -> WithingsApp:
    return WithingsApp(w.app.client_id, w.app.client_secret)


# ---------------------------------------------------------------------------------- getmeas


async def test_getmeas_returns_every_group_with_exact_decimal_values(world: World) -> None:
    body = await world.client.getmeas(world.token, category=1)
    expected = [g for g in world.user.data.measuregrps if g.get("category") == 1]
    assert {g.grpid for g in body.measuregrps} == {g["grpid"] for g in expected}
    assert body.more is False
    some = next(g for g in body.measuregrps if any(x.type == 1 for x in g.measures))
    weight = next(x for x in some.measures if x.type == 1)
    assert isinstance(weight.real, Decimal)
    assert weight.real == Decimal(weight.value).scaleb(weight.unit)
    assert weight.unit == unit_of(1)


async def test_getmeas_filters_types_and_lastupdate(world: World) -> None:
    body = await world.client.getmeas(world.token, meastypes=[1, 6], lastupdate=START - 5 * DAY)
    assert body.measuregrps
    assert all(x.type in (1, 6) for g in body.measuregrps for x in g.measures)
    assert all((g.modified or 0) > START - 5 * DAY for g in body.measuregrps)


async def test_getmeas_pages_cover_every_group_once() -> None:
    w = await _world(page_size=7)
    async with WithingsClient.open(_app(w), inner=FakeWithingsTransport(w.fake)) as client:
        pages = [p async for p in client.getmeas_pages(w.token)]
    assert len(pages) > 2
    assert [p.more for p in pages] == [True] * (len(pages) - 1) + [False]
    grpids = [g.grpid for p in pages for g in p.measuregrps]
    assert sorted(grpids) == sorted(g["grpid"] for g in w.user.data.measuregrps)
    offsets = [e.params.get("offset") for e in w.fake.log if e.endpoint == "measure:getmeas"]
    assert offsets[0] is None
    assert offsets[1:] == [str(7 * i) for i in range(1, len(pages))]


async def test_getmeas_keeps_undocumented_values_missing_fields_and_partial_records(
    quirky: World,
) -> None:
    body = await quirky.client.getmeas(quirky.token)
    groups = {g.grpid: g for g in body.measuregrps}
    source = quirky.user.data.measuregrps
    odd = groups[source[1]["grpid"]]
    assert odd.attrib == UNDOCUMENTED_ATTRIB
    assert odd.attrib not in DOCUMENTED_ATTRIB
    unknown = [x for x in groups[source[2]["grpid"]].measures if x.type == UNDOCUMENTED_MEASTYPE]
    assert unknown
    assert unknown[0].real == Decimal("424.2")
    assert groups[source[3]["grpid"]].category == 3
    bare = groups[source[4]["grpid"]]
    assert (bare.deviceid, bare.model, bare.model_id, bare.comment) == (None, None, None, None)
    partial = groups[source[5]["grpid"]]
    assert (partial.attrib, partial.category, partial.created, partial.modified) == (None,) * 4
    assert partial.measures
    assert groups[source[0]["grpid"]].measures[0].position == 99


def test_unknown_fields_are_kept_in_raw() -> None:
    group = MeasureGroup.model_validate(
        {"grpid": 1, "date": 2, "measures": [{"value": 5, "type": 1, "unit": 0, "new": [1]}],
         "brand_new_field": {"x": 1}}
    )  # fmt: skip
    assert group.raw == {"brand_new_field": {"x": 1}}
    assert group.measures[0].raw == {"new": [1]}


# ------------------------------------------------------------------------------------ heart


async def test_heart_list_pages_and_get(world: World) -> None:
    w = await _world(page_size=2, ecg_count=5)
    async with WithingsClient.open(_app(w), inner=FakeWithingsTransport(w.fake)) as client:
        pages = [p async for p in client.heart_list_pages(w.token)]
        entries = [e for p in pages for e in p.series]
        assert len(pages) == 3
        assert len(entries) == 5
        first = entries[0]
        assert first.ecg is not None
        signal = await client.heart_get(w.token, first.ecg.signalid)
    source = next(r for r in w.user.data.ecg if r["list"]["timestamp"] == first.timestamp)
    assert signal.signal == source["signal"]["signal"]
    assert signal.sampling_frequency == source["signal"]["sampling_frequency"]


async def test_heart_keeps_undocumented_and_missing_ecg_values(quirky: World) -> None:
    body = await quirky.client.heart_list(quirky.token)
    by_id = {e.ecg.signalid: e for e in body.series if e.ecg is not None}
    assert by_id[7_000].ecg is not None
    assert by_id[7_000].ecg.afib_details == 99
    assert by_id[7_000].model == 93  # absent from the spec's enum
    assert by_id[7_001].ecg is not None
    assert by_id[7_001].ecg.afib_details is None
    signal = await quirky.client.heart_get(quirky.token, 7_000)
    assert signal.wearposition == 99


# --------------------------------------------------------------------------------- activity


async def test_getactivity_by_dates_and_fields(world: World) -> None:
    first = date.fromisoformat(ymd(START - 10 * DAY))
    last = date.fromisoformat(ymd(START - 3 * DAY))
    body = await world.client.getactivity(
        world.token, startdateymd=first, enddateymd=last, data_fields=["steps"]
    )
    assert body.activities
    assert all(first.isoformat() <= a.date <= last.isoformat() for a in body.activities)
    assert all(a.steps is not None and a.calories is None for a in body.activities)
    assert all(isinstance(a.steps, Decimal) for a in body.activities)


async def test_getactivity_partial_records_are_none_not_zero(quirky: World) -> None:
    body = await quirky.client.getactivity(quirky.token, lastupdate=0)
    partial = body.activities[3]
    assert partial.hr_average is None
    assert partial.model is None
    assert partial.steps is not None


async def test_getactivity_pages(world: World) -> None:
    w = await _world(page_size=4)
    async with WithingsClient.open(_app(w), inner=FakeWithingsTransport(w.fake)) as client:
        pages = [p async for p in client.getactivity_pages(w.token, lastupdate=0)]
    assert len(pages) > 1
    assert len([a for p in pages for a in p.activities]) == len(w.user.data.activities)


async def test_getintradayactivity(world: World) -> None:
    end = max(world.user.data.intraday)
    body = await world.client.getintradayactivity(
        world.token, startdate=end - 3600, enddate=end, data_fields=["heart_rate", "spo2_auto"]
    )
    assert body.series
    assert all(end - 3600 <= ts <= end for ts in body.series)
    sample = body.series[end]
    assert sample.heart_rate == world.user.data.intraday[end]["heart_rate"]
    assert sample.steps is None  # not asked for: absent, never zero


async def test_getintradayactivity_with_no_samples(world: World) -> None:
    body = await world.client.getintradayactivity(world.token, startdate=1, enddate=100)
    assert body.series == {}


async def test_getworkouts_keeps_undocumented_category(quirky: World) -> None:
    pages = [p async for p in quirky.client.getworkouts_pages(quirky.token, lastupdate=0)]
    workouts = [x for p in pages for x in p.series]
    assert len(workouts) == len(quirky.user.data.workouts)
    assert 9_999 in {x.category for x in workouts}
    assert all(x.data is not None and isinstance(x.data.calories, Decimal) for x in workouts)


# ------------------------------------------------------------------------------------ sleep


async def test_sleep_get_series(quirky: World) -> None:
    night = quirky.user.data.sleep_series[0]["series"]
    start = night[0]["startdate"]
    body = await quirky.client.sleep_get(
        quirky.token, startdate=start, enddate=start + 6 * 3600, data_fields=["hr", "rr"]
    )
    assert body.series
    assert body.series[0].state == UNDOCUMENTED_SLEEP_STATE
    assert body.series[0].hr
    assert all(isinstance(k, int) for k in body.series[0].hr)
    assert body.series[0].snoring is None


async def test_sleep_getsummary_partial_and_unknown_fields(quirky: World) -> None:
    pages = [p async for p in quirky.client.sleep_getsummary_pages(quirky.token, lastupdate=0)]
    nights = [n for p in pages for n in p.series]
    assert len(nights) == len(quirky.user.data.sleep_summaries)
    partial = next(n for n in nights if n.data is not None and "new_metric_2027" in n.data.raw)
    assert partial.data is not None
    assert partial.data.sleep_score is not None
    assert partial.data.hr_average is None


# ------------------------------------------------------------------------------------- user


async def test_getdevice_keeps_unknown_devices_and_missing_battery(quirky: World) -> None:
    body = await quirky.client.getdevice(quirky.token)
    assert len(body.devices) == len(quirky.user.data.devices)
    assert body.devices[1].battery is None
    mystery = next(d for d in body.devices if d.model_id == 9_999)
    assert (mystery.type, mystery.battery, mystery.timezone) == ("Mystery Gadget", "critical", None)


async def test_getgoals(world: World) -> None:
    goals = (await world.client.getgoals(world.token)).goals
    assert goals.steps == 10_000
    assert goals.weight is not None
    assert goals.weight.real == Decimal("70.500")


# ----------------------------------------------------------------------------------- notify


async def test_notify_subscribe_list_update_revoke(world: World) -> None:
    c, t = world.client, world.token
    await c.notify_subscribe(t, callbackurl=HOOK, appli=1, comment="synthetic")
    await c.notify_subscribe(t, callbackurl=HOOK, appli=44)
    profiles = (await c.notify_list(t)).profiles
    pairs = {(p.appli, p.callbackurl.get_secret_value()) for p in profiles}
    assert pairs == {(1, HOOK), (44, HOOK)}
    assert "synthetic-hook-secret" not in repr(profiles)
    moved = HOOK.replace("synthetic-hook-secret", "synthetic-hook-secret-2")
    await c.notify_update(t, callbackurl=HOOK, appli=1, new_callbackurl=moved)
    urls = {p.callbackurl.get_secret_value() for p in (await c.notify_list(t, appli=1)).profiles}
    assert urls == {moved}
    await c.notify_revoke(t, callbackurl=moved, appli=1)
    assert [p.appli for p in (await c.notify_list(t)).profiles] == [44]


# ------------------------------------------------------------------------------------ OAuth


async def test_refresh_rotates_the_pair(world: World) -> None:
    tokens = await world.client.refresh(world.refresh_token)
    assert tokens.userid == world.user.userid
    assert tokens.refresh_token.get_secret_value() != world.refresh_token
    assert tokens.expires_in == 10_800
    assert world.refresh_token not in repr(tokens)
    assert tokens.access_token.get_secret_value() not in repr(tokens)
    assert all(v is None for v in tokens.raw.values())  # unknown token fields: names only
    body = await world.client.getgoals(tokens.access_token.get_secret_value())
    assert body.goals.steps == 10_000


async def test_revoke_is_signed_and_drops_the_grant(world: World) -> None:
    await world.client.revoke(world.user.userid)
    endpoints = [e.endpoint for e in world.fake.log[-2:]]
    assert endpoints == ["v2/signature:getnonce", "v2/oauth2:revoke"]
    assert world.fake.log[-1].api_status == 0
    assert (world.app.client_id, world.user.userid) not in world.fake._grants


async def test_every_call_is_a_form_post_with_no_secret_in_the_query(world: World) -> None:
    await world.client.getmeas(world.token)
    await world.client.refresh(world.refresh_token)
    await world.client.revoke(world.user.userid)
    assert world.fake.log
    assert all(e.method == "POST" and e.query_names == () for e in world.fake.log)
    assert all(e.http_status == 200 and e.api_status == 0 for e in world.fake.log)


def test_the_base_url_is_set_per_app_and_checked() -> None:
    assert WithingsApp("id", "s").base_url == "https://wbsapi.withings.net"
    for bad in (
        "http://wbsapi.withings.net",
        "https://evil.example.test",
        "https://wbsapi.withings.net/v2",
        "https://user@wbsapi.withings.net",
        "https://wbsapi.withings.net:8443",
        "https://wbsapi.withings.net?x=1",
    ):
        with pytest.raises(ValueError, match="base_url"):
            WithingsApp("id", "s", bad)
    assert "s3cr3t" not in repr(WithingsApp("id", "s3cr3t"))
