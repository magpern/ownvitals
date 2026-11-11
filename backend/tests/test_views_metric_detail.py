"""``GET /api/metrics/{key}?range=`` (issue #189): the daily series with gaps as ``null``, the
band, the Theil-Sen trend, change points only on a series that passes the minimum-data policy,
data quality, method code and provenance. Synthetic Withings groups through the real passes.
"""

from datetime import date, timedelta
from typing import Any

import pytest

from authtools import Env, auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from flows import Browser
from viewtools import (
    TODAY,
    connected_with_streams,
    days_back,
    env_fixture,  # noqa: F401 - fixture
    epoch,
    group,
    ingest,
    user,
)

SYNTHETIC = True
NOISE = (0.0, 0.1, -0.1, 0.05, -0.05)


async def detail(browser: Browser, key: str = "weight", query: str = "") -> dict[str, Any]:
    response = await browser.get(f"/api/metrics/{key}{query}")
    assert response.status_code == 200, response.text
    return response.json()


async def declining(env: Env, email: str, *, skip: tuple[int, ...] = ()) -> tuple[Browser, Any]:
    browser, owner = await user(env, email)
    connection = connected_with_streams(owner)
    days = days_back(120, skip=skip)
    weights = {d: 90.0 - 0.05 * n + NOISE[n % 5] for n, d in enumerate(days)}
    await ingest(env, owner, connection, weights)
    return browser, owner


async def test_gaps_are_null_never_zero(env: Env) -> None:
    browser, _ = await declining(env, "a@example.com", skip=(5, 6, 7, 30))
    body = await detail(browser)
    series = body["series"]
    assert len(series) == 90
    assert body["range"] == "90d"
    assert series[0]["day"] == (TODAY - timedelta(days=89)).isoformat()
    assert series[-1]["day"] == TODAY.isoformat()
    by_day = {p["day"]: p for p in series}
    for offset in (5, 6, 7, 30):
        gap = by_day[(TODAY - timedelta(days=offset)).isoformat()]
        assert gap["value"] is None
        assert gap["n"] == 0
        assert gap["method"] is None
    assert by_day[TODAY.isoformat()]["value"] is None  # nothing recorded today
    assert all(p["value"] != 0 for p in series)
    recorded = [p for p in series if p["value"] is not None]
    assert len(recorded) == 90 - 4 - 1
    assert all(p["method"] == "morning" and p["n"] == 1 for p in recorded)
    quality = body["data_quality"]
    assert quality["n_days"] == len(recorded)
    assert quality["longest_gap_days"] == 3
    assert [g["days"] for g in quality["gaps"]] == [3]  # only runs of 3 or more


async def test_the_trend_the_band_and_the_method(env: Env) -> None:
    browser, _ = await declining(env, "b@example.com")
    body = await detail(browser, query="?range=90d")
    trend = body["trend"]
    assert trend["state"] == "ok"
    assert trend["direction"] == "decreasing"
    assert trend["significant"] is True
    assert trend["alpha"] == 0.05
    assert trend["correction"] == "ar1"
    assert trend["slope_low"] <= trend["slope_per_day"] <= trend["slope_high"]
    assert trend["slope_per_day"] == pytest.approx(-0.05, abs=0.01)
    band = body["band"]
    assert band["low"] < band["median"] < band["high"]
    assert band["k"] == 1.5
    assert body["state"] == "up_to_date"
    assert body["band_progress"] is None
    method = body["method"]
    assert method["code"] == "daily_summary"
    assert "text" not in method
    assert method["parameters"]["band_k"] == 1.5
    assert method["algo_versions"]["daily"] == "daily/1"
    provenance = body["provenance"]
    assert provenance["algo_versions"] == ["daily/1"]
    assert provenance["recompute_pending"] is False
    assert provenance["input_revision"] == provenance["current_revision"]
    assert body["latest"]["day"] == (TODAY - timedelta(days=1)).isoformat()


async def test_change_points_are_found_on_a_step_in_a_series_that_passes_the_policy(
    env: Env,
) -> None:
    browser, owner = await user(env, "c@example.com")
    connection = connected_with_streams(owner)
    days = days_back(100)
    weights = {d: (85.0 if n < 60 else 82.0) + NOISE[n % 5] for n, d in enumerate(days)}
    await ingest(env, owner, connection, weights)
    body = await detail(browser, query="?range=90d")
    found = body["change_points"]
    assert found["state"] == "ok"
    assert found["searched"] is True
    step = days[60]
    near = min(found["points"], key=lambda p: abs((date.fromisoformat(p["day"]) - step).days))
    assert abs((date.fromisoformat(near["day"]) - step).days) <= 3, found
    assert near["mean_after"] < near["mean_before"]
    assert near["shift"] < -2.0
    # 100 days of data span only 54 % of a 6-month window: below the trend policy, no search
    wide = await detail(browser, query="?range=6mo")
    assert wide["change_points"]["state"] == "insufficient_data"
    assert wide["change_points"]["points"] == []
    assert wide["trend"]["state"] == "insufficient_data"
    assert len(wide["series"]) == 182


async def test_a_series_below_the_policy_gets_no_trend_and_no_change_points(env: Env) -> None:
    browser, owner = await user(env, "d@example.com")
    connection = connected_with_streams(owner)
    await ingest(env, owner, connection, pulses=dict.fromkeys(days_back(4), 60))
    body = await detail(browser, "heart_pulse")
    assert body["trend"]["state"] == "insufficient_data"
    assert body["trend"]["slope_per_day"] is None
    assert body["trend"]["direction"] is None
    assert body["trend"]["progress"]["days_required"] == 8
    assert body["change_points"] == {
        "state": "insufficient_data",
        "progress": body["change_points"]["progress"],
        "searched": False,
        "points": [],
        "algo_version": None,
    }
    assert body["change_points"]["progress"]["days_with_data"] == 4
    assert body["band"] is None
    assert body["band_progress"]["days_required"] == 10
    assert body["state"] == "insufficient_data"
    assert len([p for p in body["series"] if p["value"] is not None]) == 4


async def test_a_constant_series_still_gets_a_band_of_the_minimum_width(env: Env) -> None:
    browser, owner = await user(env, "const@example.com")
    connection = connected_with_streams(owner)
    await ingest(env, owner, connection, dict.fromkeys(days_back(100), 80.0))
    body = await detail(browser, query="?range=90d")
    band = body["band"]
    assert band["median"] == 80.0
    assert band["low"] == pytest.approx(79.55)
    assert band["high"] == pytest.approx(80.45)
    assert body["method"]["parameters"]["band_min_half_width"] == 0.45


@pytest.mark.parametrize(
    ("query", "days"), [("", 90), ("?range=30d", 30), ("?range=1y", 365), ("?range=6mo", 182)]
)
async def test_the_ranges(env: Env, query: str, days: int) -> None:
    browser, _ = await declining(env, "e@example.com")
    body = await detail(browser, query=query)
    assert len(body["series"]) == days
    assert body["end"] == TODAY.isoformat()


async def test_range_all_starts_at_the_first_day_with_data(env: Env) -> None:
    browser, _ = await declining(env, "f@example.com")
    body = await detail(browser, query="?range=all")
    assert body["start"] == (TODAY - timedelta(days=120)).isoformat()
    assert len(body["series"]) == 121
    assert body["series"][0]["value"] is not None


async def test_invalid_ranges_and_unknown_metrics(env: Env) -> None:
    browser, _ = await user(env, "g@example.com")
    for bad in ("?range=2y", "?range=", "?range=90d;drop", "?range=ALL"):
        assert (await browser.get(f"/api/metrics/weight{bad}")).status_code == 422, bad
    for key in ("no_such_metric", "Weight", "weight%3Bdrop", "segmental_muscle_mass:left_arm"):
        assert (await browser.get(f"/api/metrics/{key}")).status_code == 404, key


async def test_unconfirmed_readings_are_shown_apart_and_never_used(env: Env) -> None:
    browser, owner = await user(env, "h@example.com")
    connection = connected_with_streams(owner)
    days = days_back(30)
    extra = group(987_900_001, epoch(days[10], 8, 0), (1, 55_000, -3, None), attrib=1)
    await ingest(
        env, owner, connection, dict.fromkeys(days, 80.0), extra=[extra], first_grpid=987_500_000
    )
    body = await detail(browser, query="?range=30d")
    assert body["unconfirmed"] == [{"day": days[10].isoformat(), "n": 1}]
    that_day = {p["day"]: p for p in body["series"]}[days[10].isoformat()]
    assert that_day["value"] == 80.0
    assert that_day["n"] == 1


async def test_another_users_series_is_not_readable(env: Env) -> None:
    alice, alice_id = await user(env, "alice@example.com")
    bob, _ = await user(env, "bob@example.com")
    await ingest(
        env, alice_id, connected_with_streams(alice_id), dict.fromkeys(days_back(40), 80.0)
    )
    mine = await detail(alice, query="?range=30d")
    theirs = await detail(bob, query="?range=30d")
    assert any(p["value"] is not None for p in mine["series"])
    assert all(p["value"] is None for p in theirs["series"])
    assert theirs["latest"] is None
    assert theirs["provenance"]["algo_versions"] == []
