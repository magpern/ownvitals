"""Data endpoints of the Withings fake: pagination (``more``/``offset``), ``lastupdate``, filters,
``data_fields``, the 24 h windows, deletions and backdated edits between reads, and a mutation
between two pages. Synthetic data only; no network.
"""

from typing import Any

import pytest

import withingsstore as store
from withingsfake import call, fake_client, make_fake
from withingsstreams import DAY
from withingswire import STATUS_BAD_PARAMS


class Env:
    def __init__(self, **dataset: Any) -> None:
        options = {"days": 40, "ecg_count": 3} | dataset
        self.fake, self.app, self.user = make_fake(seed=6, **options)
        self.client = fake_client(self.fake)
        self.token = self.fake.tokens_for(self.app.client_id, self.user.userid)["access_token"]
        self.data = self.user.data

    async def get(self, path: str, action: str, **params: Any) -> dict[str, Any]:
        return await call(self.client, path, action, self.token, **params)

    async def meas(self, **params: Any) -> dict[str, Any]:
        reply = await self.get("/measure", "getmeas", **params)
        assert reply["status"] == 0, reply
        return reply["body"]

    async def all_groups(self, **params: Any) -> list[dict[str, Any]]:
        """Follow ``more``/``offset`` to the end."""
        rows: list[dict[str, Any]] = []
        offset = 0
        for _ in range(500):
            body = await self.meas(**params, offset=offset)
            rows += body["measuregrps"]
            if not body["more"]:
                return rows
            offset = body["offset"]
        raise AssertionError("pagination does not end")

    @property
    def now(self) -> int:
        return int(self.fake.clock.now())


@pytest.fixture
def env() -> Env:
    return Env()


def _group(date: int, weight: float = 70.0) -> dict[str, Any]:
    return {"date": date, "measures": [{"value": int(weight * 1000), "type": 1, "unit": -3}]}


async def test_pages_follow_more_and_offset_until_the_end(env: Env) -> None:
    everything = await env.meas(lastupdate=0)
    total = len(everything["measuregrps"])
    assert total > 20
    env.fake.page_size = 7
    first = await env.meas(lastupdate=0)
    assert len(first["measuregrps"]) == 7
    assert first["more"] == 1
    assert first["offset"] == 7
    assert isinstance(first["more"], int)
    assert not isinstance(first["more"], bool)
    paged = await env.all_groups(lastupdate=0)
    assert [g["grpid"] for g in paged] == [g["grpid"] for g in everything["measuregrps"]]
    last = await env.meas(lastupdate=0, offset=(total // 7) * 7)
    assert last["more"] == 0
    assert last["offset"] == 0


async def test_a_full_last_page_has_no_more(env: Env) -> None:
    total = len((await env.meas(lastupdate=0))["measuregrps"])
    env.fake.page_size = total
    exact = await env.meas(lastupdate=0)
    assert (len(exact["measuregrps"]), exact["more"], exact["offset"]) == (total, 0, 0)
    env.fake.page_size = total - 1
    almost = await env.meas(lastupdate=0)
    assert (almost["more"], almost["offset"]) == (1, total - 1)
    rest = await env.meas(lastupdate=0, offset=almost["offset"])
    assert (len(rest["measuregrps"]), rest["more"]) == (1, 0)


async def test_results_are_newest_first(env: Env) -> None:
    dates = [g["date"] for g in (await env.meas())["measuregrps"]]
    assert dates == sorted(dates, reverse=True)


async def test_other_endpoints_report_more_as_a_boolean(env: Env) -> None:
    env.fake.page_size = 2
    heart = (await env.get("/v2/heart", "list"))["body"]
    assert heart["more"] is True
    assert len(heart["series"]) == 2
    rest = (await env.get("/v2/heart", "list", offset=heart["offset"]))["body"]
    assert rest["more"] is False
    assert rest["offset"] == 0
    assert len(heart["series"]) + len(rest["series"]) == 3
    window = {"startdateymd": "2000-01-01", "enddateymd": "2100-01-01"}
    activity = (await env.get("/v2/measure", "getactivity", **window))["body"]
    assert activity["more"] is True
    assert isinstance(activity["offset"], int)


async def test_a_page_size_can_be_set_per_endpoint(env: Env) -> None:
    env.fake.page_sizes["measure:getmeas"] = 3
    assert len((await env.meas())["measuregrps"]) == 3
    assert len((await env.get("/v2/heart", "list"))["body"]["series"]) == 3  # all of them


async def test_lastupdate_returns_what_changed_after_it(env: Env) -> None:
    first = await env.meas(lastupdate=0)
    mark = first["updatetime"]
    assert mark == env.now
    assert (await env.meas(lastupdate=mark))["measuregrps"] == []
    env.fake.advance(60)
    added = store.add_group(env.data, _group(env.now - 3600), now=env.now)
    changed = await env.meas(lastupdate=mark)
    assert [g["grpid"] for g in changed["measuregrps"]] == [added["grpid"]]
    assert changed["updatetime"] == mark + 60
    assert (await env.meas(lastupdate=env.now))["measuregrps"] == []  # strictly after


async def test_a_backdated_insert_is_found_by_lastupdate_but_not_by_an_old_window(env: Env) -> None:
    mark = (await env.meas())["updatetime"]
    old = env.now - 30 * DAY
    env.fake.advance(120)
    added = store.add_group(env.data, _group(old, 64.0), now=env.now)
    assert added["date"] == old
    assert added["created"] == added["modified"] == env.now
    found = await env.meas(lastupdate=mark)
    assert [g["grpid"] for g in found["measuregrps"]] == [added["grpid"]]
    recent = await env.meas(startdate=env.now - 7 * DAY, enddate=env.now)
    assert added["grpid"] not in {g["grpid"] for g in recent["measuregrps"]}
    window = await env.meas(startdate=old, enddate=old)
    assert added["grpid"] in {g["grpid"] for g in window["measuregrps"]}


async def test_an_edit_moves_modified_but_not_date(env: Env) -> None:
    target = (await env.meas())["measuregrps"][3]
    mark = env.now
    env.fake.advance(300)
    edited = store.edit_group(
        env.data, target["grpid"], now=env.now, measures=[{"value": 61_000, "type": 1, "unit": -3}]
    )
    assert edited["date"] == target["date"]
    found = (await env.meas(lastupdate=mark))["measuregrps"]
    assert [g["grpid"] for g in found] == [target["grpid"]]
    assert found[0]["measures"] == [{"value": 61_000, "type": 1, "unit": -3}]
    assert found[0]["modified"] == env.now


async def test_a_deleted_group_just_stops_being_returned(env: Env) -> None:
    before = await env.all_groups(lastupdate=0)
    victim = before[5]["grpid"]
    mark = env.now
    env.fake.advance(60)
    store.delete_group(env.data, victim)
    assert env.data.deleted_grpids == [victim]
    after = await env.all_groups(lastupdate=0)
    assert victim not in {g["grpid"] for g in after}
    assert len(after) == len(before) - 1
    assert (await env.meas(lastupdate=mark))["measuregrps"] == []  # nothing reports the deletion


async def test_a_deletion_between_two_pages_skips_a_row(env: Env) -> None:
    env.fake.page_size = 5
    before = [g["grpid"] for g in await env.all_groups(lastupdate=0)]
    victim = before[2]  # on the first page
    env.fake.inject(
        "mutate",
        at=env.fake.endpoint_counts["measure:getmeas"] + 2,  # the second page of the next read
        endpoint="measure:getmeas",
        action=lambda _: store.delete_group(env.data, victim),
    )
    pages = [g["grpid"] for g in await env.all_groups(lastupdate=0)]
    skipped = set(before) - set(pages) - {victim}
    assert len(skipped) == 1  # index based paging: everything after the victim moved up by one
    assert before[5] in skipped


async def test_an_insertion_between_two_pages_repeats_a_row(env: Env) -> None:
    env.fake.page_size = 5
    before = [g["grpid"] for g in await env.all_groups(lastupdate=0)]
    newest = max(g["date"] for g in env.data.measuregrps) + 60
    env.fake.inject(
        "mutate",
        at=env.fake.endpoint_counts["measure:getmeas"] + 2,  # the second page of the next read
        endpoint="measure:getmeas",
        action=lambda f: store.add_group(env.data, _group(newest), now=int(f.clock.now())),
    )
    pages = [g["grpid"] for g in await env.all_groups(lastupdate=0)]
    assert (
        pages.count(before[4]) == 2
    )  # the last row of page one comes back as the first of page two


async def test_filters_by_type_category_and_date(env: Env) -> None:
    only = (await env.meas(meastype=1))["measuregrps"]
    assert only
    assert all(m["type"] == 1 for g in only for m in g["measures"])
    several = (await env.meas(meastypes="9,10,11"))["measuregrps"]
    assert several
    assert {m["type"] for g in several for m in g["measures"]} <= {9, 10, 11}
    objectives = (await env.meas(category=2))["measuregrps"]
    assert objectives
    assert {g["category"] for g in objectives} == {2}
    real = (await env.meas(category=1))["measuregrps"]
    assert {g["category"] for g in real} == {1}
    dates = sorted(g["date"] for g in env.data.measuregrps)
    window = (await env.meas(startdate=dates[3], enddate=dates[6]))["measuregrps"]
    assert {g["date"] for g in window} == set(dates[3:7])  # both ends inclusive
    ignored = await env.meas(lastupdate=0, startdate=dates[3], enddate=dates[3])
    assert len(ignored["measuregrps"]) == len(dates)  # lastupdate replaces the date range


async def test_missing_or_malformed_parameters_are_invalid_params(env: Env) -> None:
    window = {"startdateymd": "2000-01-01", "enddateymd": "2100-01-01"}
    cases = [
        ("/measure", "getmeas", {"offset": "x"}),
        ("/measure", "getmeas", {"offset": -1}),
        ("/measure", "getmeas", {"meastypes": "1,a"}),
        ("/v2/measure", "getactivity", {}),
        ("/v2/measure", "getactivity", {"startdateymd": "2026-01-01"}),
        ("/v2/measure", "getactivity", {"startdateymd": "01/02/2026", "enddateymd": "2026-02-01"}),
        ("/v2/measure", "getworkouts", {}),
        ("/v2/sleep", "getsummary", {}),
        ("/v2/sleep", "get", {"startdate": 1}),
        ("/v2/heart", "get", {}),
        ("/v2/heart", "get", {"signalid": 99_999}),
        ("/v2/heart", "list", {"startdate": "x"}),
    ]
    for path, action, params in cases:
        assert (await env.get(path, action, **params))["status"] == STATUS_BAD_PARAMS, (
            action,
            params,
        )
    assert (await env.get("/v2/measure", "getactivity", **window))["status"] == 0


async def test_heart_list_filters_and_get_returns_the_signal(env: Env) -> None:
    listed = (await env.get("/v2/heart", "list"))["body"]["series"]
    stamps = [e["timestamp"] for e in listed]
    assert stamps == sorted(stamps, reverse=True)
    latest = (await env.get("/v2/heart", "list", startdate=stamps[0]))["body"]["series"]
    assert [e["timestamp"] for e in latest] == [stamps[0]]
    got = (await env.get("/v2/heart", "get", signalid=listed[0]["ecg"]["signalid"]))["body"]
    assert got["sampling_frequency"] in (300, 500)
    assert all(isinstance(v, int) for v in got["signal"])
    assert got["heart_rate"]["value"] == listed[0]["heart_rate"]


async def test_activity_data_fields_keep_the_base_fields_and_the_requested_ones(env: Env) -> None:
    window = {"startdateymd": "2000-01-01", "enddateymd": "2100-01-01"}
    full = (await env.get("/v2/measure", "getactivity", **window))["body"]["activities"][0]
    some = (await env.get("/v2/measure", "getactivity", data_fields="steps,calories", **window))[
        "body"
    ]
    row = some["activities"][0]
    assert "hr_average" in full
    assert "hr_average" not in row
    assert row["steps"] == full["steps"]
    assert row["calories"] == full["calories"]
    assert row["date"] == full["date"]
    assert row["modified"] == full["modified"]


async def test_activity_date_range_and_lastupdate(env: Env) -> None:
    days = sorted(a["date"] for a in env.data.activities)
    one = (await env.get("/v2/measure", "getactivity", startdateymd=days[2], enddateymd=days[4]))[
        "body"
    ]
    assert [a["date"] for a in one["activities"]] == days[2:5]
    since = max(a["modified"] for a in env.data.activities) - 1
    newest = (await env.get("/v2/measure", "getactivity", lastupdate=since))["body"]["activities"]
    assert [a["date"] for a in newest] == [days[-1]]
    nothing = await env.get("/v2/measure", "getactivity", lastupdate=since + 1)
    assert nothing["body"]["activities"] == []  # strictly after


async def test_intraday_returns_at_most_24_hours_after_startdate() -> None:
    env = Env(intraday_days=3)
    stamps = sorted(env.data.intraday)
    assert stamps[-1] - stamps[0] > 2 * DAY
    series = (
        await env.get("/v2/measure", "getintradayactivity", startdate=stamps[0], enddate=stamps[-1])
    )["body"]["series"]
    got = sorted(int(t) for t in series)
    assert got
    assert got[-1] - stamps[0] <= DAY
    latest = (await env.get("/v2/measure", "getintradayactivity"))["body"]["series"]
    assert all(stamps[-1] - int(t) <= DAY for t in latest)
    slim = (await env.get("/v2/measure", "getintradayactivity", data_fields="heart_rate"))["body"][
        "series"
    ]
    sample = next(iter(slim.values()))
    assert "steps" not in sample
    assert sample["heart_rate"] > 0
    assert sample["model_id"] == 93


async def test_sleep_get_needs_a_window_and_gates_the_series_by_data_fields(env: Env) -> None:
    segment = env.data.sleep_series[0]["series"][0]
    window = {"startdate": segment["startdate"], "enddate": segment["enddate"]}
    bare = (await env.get("/v2/sleep", "get", **window))["body"]["series"]
    assert bare
    assert "hr" not in bare[0]
    assert {"startdate", "enddate", "state"} <= set(bare[0])
    asked = (await env.get("/v2/sleep", "get", data_fields="hr,rmssd", **window))["body"]["series"]
    assert set(asked[0]["hr"]) <= {str(t) for t in range(window["startdate"], window["enddate"])}
    assert "rmssd" in asked[0]
    assert "rr" not in asked[0]


async def test_sleep_get_returns_the_first_24_hours_of_a_longer_window() -> None:
    env = Env(sleep_series_nights=5)
    starts = sorted(seg["startdate"] for n in env.data.sleep_series for seg in n["series"])
    assert starts[-1] >= starts[0] + DAY  # the data reaches further than a day
    far = {"startdate": starts[0], "enddate": starts[0] + 5 * DAY}
    got = (await env.get("/v2/sleep", "get", data_fields="hr", **far))["body"]["series"]
    assert got
    assert max(s["startdate"] for s in got) < starts[0] + DAY
    assert {t for s in got for t in s["hr"]} <= {str(t) for t in range(starts[0], starts[0] + DAY)}


async def test_sleep_summary_and_workouts_honour_data_fields_and_lastupdate(env: Env) -> None:
    window = {"startdateymd": "2000-01-01", "enddateymd": "2100-01-01"}
    summary = (await env.get("/v2/sleep", "getsummary", data_fields="sleep_score", **window))[
        "body"
    ]
    assert summary["series"]
    assert all(set(r["data"]) <= {"sleep_score"} for r in summary["series"])
    assert summary["series"][0]["model"] in (16, 32)
    newest = max(r["modified"] for r in env.data.sleep_summaries)
    recent = (await env.get("/v2/sleep", "getsummary", lastupdate=newest - 1))["body"]["series"]
    assert [r["modified"] for r in recent] == [newest]
    workouts = (await env.get("/v2/measure", "getworkouts", data_fields="calories", lastupdate=0))[
        "body"
    ]
    assert workouts["series"]
    assert all(set(w["data"]) <= {"calories"} for w in workouts["series"])


async def test_devices_and_goals_are_the_users_own(env: Env) -> None:
    devices = (await env.get("/v2/user", "getdevice"))["body"]["devices"]
    assert {d["type"] for d in devices} == {
        "Scale",
        "Blood Pressure Monitor",
        "Activity Tracker",
        "Sleep Monitor",
    }
    assert (await env.get("/v2/user", "getgoals"))["body"]["goals"]["steps"] == 10_000


async def test_each_user_sees_only_their_own_data() -> None:
    env = Env()
    other = env.fake.add_user(days=40, ecg_count=1)
    token = env.fake.tokens_for(env.app.client_id, other.userid)["access_token"]
    mine = {g["grpid"] for g in (await env.meas(lastupdate=0))["measuregrps"]}
    theirs = (await call(env.client, "/measure", "getmeas", token, lastupdate=0))["body"][
        "measuregrps"
    ]
    assert len(theirs) == len(other.data.measuregrps)
    assert theirs
    assert [g["date"] for g in theirs] != [g["date"] for g in env.data.measuregrps]
    assert len(mine) == len(env.data.measuregrps)
