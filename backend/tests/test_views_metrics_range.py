"""``GET /api/metrics?range=`` (issue 329): the overview tiles' sparklines span the same
windows as the metric detail; the read guards are unchanged. Synthetic data only."""

from typing import Any

import pytest

from authtools import Env, auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from flows import Browser
from viewtools import (
    TODAY,
    connected_with_streams,
    days_back,
    env_fixture,  # noqa: F401 - fixture
    ingest,
    user,
)

SYNTHETIC = True


async def weight_tile(browser: Browser, query: str = "") -> dict[str, Any]:
    response = await browser.get(f"/api/metrics{query}")
    assert response.status_code == 200, response.text
    return next(m for m in response.json()["metrics"] if m["key"] == "weight")


@pytest.mark.parametrize(
    ("range_name", "days"), [("7d", 7), ("30d", 30), ("90d", 90), ("6mo", 182), ("1y", 365)]
)
async def test_sparkline_spans_the_requested_range(env: Env, range_name: str, days: int) -> None:
    browser, owner = await user(env, "a@example.com")
    await ingest(env, owner, connected_with_streams(owner), dict.fromkeys(days_back(60), 80.0))
    tile = await weight_tile(browser, f"?range={range_name}")
    detail = (await browser.get(f"/api/metrics/weight?range={range_name}")).json()
    assert len(tile["sparkline"]) == days == len(detail["series"])
    assert tile["sparkline"][0]["day"] == detail["start"]
    assert tile["sparkline"][-1]["day"] == detail["end"]


async def test_all_starts_at_the_first_day_with_data_and_a_bare_overview_keeps_30_days(
    env: Env,
) -> None:
    browser, owner = await user(env, "a@example.com")
    await ingest(env, owner, connected_with_streams(owner), dict.fromkeys(days_back(60), 80.0))
    tile = await weight_tile(browser, "?range=all")
    assert tile["sparkline"][0]["day"] == days_back(60)[0].isoformat()
    assert tile["sparkline"][-1]["day"] == TODAY.isoformat()
    assert len((await weight_tile(browser))["sparkline"]) == 30


async def test_range_does_not_change_the_band_and_a_metric_without_data_stays_empty(
    env: Env,
) -> None:
    browser, owner = await user(env, "a@example.com")
    await ingest(env, owner, connected_with_streams(owner), dict.fromkeys(days_back(60), 80.0))
    short = await weight_tile(browser, "?range=30d")
    long = await weight_tile(browser, "?range=1y")
    assert short["typical_range"] == long["typical_range"]
    response = await browser.get("/api/metrics?range=1y")
    esc = next(m for m in response.json()["metrics"] if m["key"] == "esc")
    assert esc["sparkline"] == []


async def test_invalid_range_is_refused(env: Env) -> None:
    browser, _ = await user(env, "a@example.com")
    for bad in ("?range=2y", "?range=", "?range=ALL", "?range=30d;x"):
        assert (await browser.get(f"/api/metrics{bad}")).status_code == 422, bad


async def test_another_users_tiles_are_not_readable_whatever_the_range(env: Env) -> None:
    alice, alice_id = await user(env, "alice@example.com")
    bob, _ = await user(env, "bob@example.com")
    await ingest(
        env, alice_id, connected_with_streams(alice_id), dict.fromkeys(days_back(40), 80.0)
    )
    mine = await weight_tile(alice, "?range=1y")
    theirs = await weight_tile(bob, "?range=1y")
    assert any(p["value"] is not None for p in mine["sparkline"])
    assert theirs["latest"] is None
    assert theirs["sparkline"] == []


async def test_anonymous_request_with_a_range_is_refused(env: Env) -> None:
    assert (await env.client.get("/api/metrics?range=1y")).status_code == 401
