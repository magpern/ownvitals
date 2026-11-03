"""``GET /api/status`` and ``GET /api/metrics`` (issue #189): every data state of the design is
produced from synthetic Withings groups and cursors through the real ingest, normalisation and
daily passes, and read back as the signed-in user over the app role under forced RLS. A stream
that is not supported (or not shared, not available) is never "no data". Synthetic data only.
"""

from datetime import timedelta
from typing import Any

import pytest

from authtools import Env, auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from flows import Browser, reason
from ownvitals.views.states import STATES
from viewtools import (
    NOW,
    TODAY,
    connected_with_streams,
    days_back,
    env_fixture,  # noqa: F401 - fixture
    ingest,
    plant_connection,
    plant_cursor,
    plant_import_job,
    run,
    user,
)

SYNTHETIC = True
WEIGHTS = {day: 80.0 + (n % 5) * 0.1 for n, day in enumerate(days_back(60))}
OLD = NOW - timedelta(days=5)


async def tile(browser: Browser, key: str) -> dict[str, Any]:
    response = await browser.get("/api/metrics")
    assert response.status_code == 200, response.text
    found = [m for m in response.json()["metrics"] if m["key"] == key]
    assert len(found) == 1
    return found[0]


async def seeded(env: Env, email: str, **streams: Any) -> tuple[Browser, Any, Any]:
    browser, owner = await user(env, email)
    connection = connected_with_streams(owner, **streams)
    await ingest(env, owner, connection, WEIGHTS)
    return browser, owner, connection


async def test_anonymous_requests_are_refused(env: Env) -> None:
    for path in ("/api/status", "/api/metrics", "/api/metrics/weight", "/api/nerve/summary"):
        response = await env.client.get(path)
        assert (response.status_code, reason(response)) == (401, "not_authenticated"), path


async def test_up_to_date_with_a_typical_range_a_sparkline_and_the_latest_value(env: Env) -> None:
    browser, _, _ = await seeded(env, "a@example.com")
    weight = await tile(browser, "weight")
    assert weight["state"] == "up_to_date"
    assert weight["area"] == "body"
    assert weight["unit"] == "kg"
    assert weight["stream"] == "measures"
    assert weight["latest"]["day"] == (TODAY - timedelta(days=1)).isoformat()
    assert weight["latest"]["method"] == "morning"
    band = weight["typical_range"]
    assert band["k"] == 1.5
    assert band["window_days"] == 90
    assert band["low"] < band["median"] < band["high"]
    assert len(weight["sparkline"]) == 30
    assert weight["sparkline"][-1]["value"] is None  # nothing recorded today: a gap, not 0
    assert weight["sparkline"][-2]["value"] is not None
    assert weight["sparkline"][0]["value"] is not None
    # a metric the user never recorded has no value, no sparkline and no invented zero
    nothing = await tile(browser, "esc")
    assert nothing["latest"] is None
    assert nothing["sparkline"] == []
    assert nothing["state"] == "insufficient_data"
    assert nothing["progress"]["days_with_data"] == 0


async def test_insufficient_data_carries_its_progress_toward_the_minimum(env: Env) -> None:
    browser, owner = await user(env, "b@example.com")
    connection = connected_with_streams(owner)
    await ingest(env, owner, connection, pulses=dict.fromkeys(days_back(3), 61))
    pulse = await tile(browser, "heart_pulse")
    assert pulse["state"] == "insufficient_data"
    progress = pulse["progress"]
    assert (progress["days_with_data"], progress["days_required"]) == (3, 10)
    assert progress["span_required"] == 21
    assert "days_with_data" in progress["shortfalls"]
    assert 0 < progress["fraction"] < 1
    assert pulse["typical_range"] is None  # no band below the policy
    assert pulse["latest"]["value"] == 61.0  # the value itself is still shown


# state, cursor overrides, connection arguments, metric, expected progress
CASES = [
    ("stale", {"measures": {"last_ok": OLD}}, {}, "weight", None),
    (
        "importing",
        {"measures": {"status": "running", "index": 3}},
        {},
        "weight",
        16,  # 3 of the 18 one-year windows from 2009 to 2026
    ),
    ("incomplete", {"measures": {"status": "running", "index": 3}}, {}, "weight", 16),
    ("not_shared", {"measures": {"capability": "not_authorized"}}, {}, "weight", None),
    ("not_supported", {"measures": {"capability": "unsupported"}}, {}, "weight", None),
    ("not_available", {"ecg": {"capability": "not_available"}}, {}, "qtc_interval", None),
    ("not_supported", {"ecg": {"capability": "unsupported"}}, {}, "pr_interval", None),
    ("needs_reconnect", {}, {"status": "needs_reconnect"}, "weight", None),
    (
        "withings_plus_needed",
        {},
        {"last_error": "blocked_by_provider_plan"},
        "weight",
        None,
    ),
    ("not_connected", {}, {"status": "disconnected"}, "weight", None),
]


@pytest.mark.parametrize(("state", "cursors", "connection", "key", "percent"), CASES)
async def test_each_data_state_is_produced_from_the_stream_and_the_connection(
    env: Env, state: str, cursors: dict[str, Any], connection: dict[str, Any], key: str, percent
) -> None:
    browser, owner = await user(env, "c@example.com")
    link = plant_connection(owner, **connection)
    for stream in ("measures", "ecg"):
        plant_cursor(owner, link, stream, **cursors.get(stream, {}))
    if state == "importing":
        plant_import_job(owner, link, "measures")
    await ingest(env, owner, link, WEIGHTS)
    found = await tile(browser, key)
    assert found["state"] == state, found
    assert found["progress_percent"] == percent
    assert state in STATES
    if state in {"not_shared", "not_supported", "not_available"}:
        assert found["progress"] is None  # never "insufficient data", never a count


async def test_a_stream_that_is_not_supported_is_never_no_data(env: Env) -> None:
    browser, owner = await user(env, "d@example.com")
    link = plant_connection(owner)
    plant_cursor(owner, link, "measures", capability="supported")
    plant_cursor(owner, link, "ecg", capability="unsupported")
    unsupported = await tile(browser, "pr_interval")
    assert unsupported["state"] == "not_supported"
    assert unsupported["latest"] is None
    assert unsupported["progress"] is None
    assert unsupported["typical_range"] is None
    assert unsupported["sparkline"] == []
    supported = await tile(browser, "esc")  # the same user, a supported stream with no data
    assert supported["state"] == "insufficient_data"


async def test_a_pending_recompute_is_stale_not_wrong(env: Env) -> None:
    browser, owner, _ = await seeded(env, "e@example.com")
    assert (await tile(browser, "weight"))["state"] == "up_to_date"
    run(
        "UPDATE data_revisions SET revision = revision + 1 WHERE owner_id = :o "
        "AND stream = 'measures'",
        o=owner,
    )
    found = await tile(browser, "weight")
    assert found["state"] == "stale"
    assert found["latest"] is not None  # the last good value is still shown


async def test_status_without_a_connection(env: Env) -> None:
    browser, _ = await user(env, "f@example.com")
    body = (await browser.get("/api/status")).json()
    assert body["connection"] == "not_connected"
    assert body["streams"] == []
    assert body["importing"] == []
    assert body["broken_connections"] == []
    assert body["last_reading_at"] is None
    assert body["last_sync_at"] is None


async def test_status_lists_every_stream_with_its_state(env: Env) -> None:
    browser, owner, connection = await seeded(env, "g@example.com")
    plant_cursor(owner, connection, "activity_daily", capability="not_authorized")
    plant_cursor(owner, connection, "workouts", capability="unsupported")
    plant_cursor(owner, connection, "sleep_summary", status="running", index=2)
    plant_import_job(owner, connection, "sleep_summary")
    plant_cursor(owner, connection, "sleep_series", status="running", index=1)
    plant_cursor(owner, connection, "intraday", capability="not_available", last_ok=OLD)
    body = (await browser.get("/api/status")).json()
    by_stream = {s["stream"]: s for s in body["streams"]}
    assert {name: s["state"] for name, s in by_stream.items()} == {
        "measures": "up_to_date",
        "ecg": "up_to_date",
        "activity_daily": "not_shared",
        "workouts": "not_supported",
        "sleep_summary": "importing",
        "sleep_series": "incomplete",
        "intraday": "not_available",
    }
    assert by_stream["sleep_summary"]["progress_percent"] is not None
    assert body["importing"] == ["sleep_summary"]
    assert body["incomplete"] == ["sleep_series"]
    assert body["connection"] == "connected"
    assert body["broken_connections"] == []
    assert body["last_reading_at"].startswith((TODAY - timedelta(days=1)).isoformat())
    assert body["last_sync_at"] is not None
    assert by_stream["measures"]["capability"] == "supported"


@pytest.mark.parametrize(
    ("connection", "expected"),
    [
        ({"status": "needs_reconnect"}, "needs_reconnect"),
        ({"last_error": "blocked_by_provider_plan"}, "withings_plus_needed"),
    ],
)
async def test_status_names_a_broken_connection(
    env: Env, connection: dict[str, Any], expected: str
) -> None:
    browser, owner = await user(env, "h@example.com")
    link = plant_connection(owner, **connection)
    plant_cursor(owner, link, "measures")
    body = (await browser.get("/api/status")).json()
    assert body["connection"] == expected
    assert body["broken_connections"] == ["withings"]
    assert [s["state"] for s in body["streams"]] == [expected]


async def test_a_user_sees_only_their_own_metrics_and_status(env: Env) -> None:
    alice, alice_id = await user(env, "alice@example.com")
    bob, _ = await user(env, "bob@example.com")
    link = connected_with_streams(alice_id)
    await ingest(env, alice_id, link, WEIGHTS)
    assert (await tile(alice, "weight"))["latest"] is not None
    other = await tile(bob, "weight")
    assert other["latest"] is None
    assert other["state"] == "not_connected"
    assert (await bob.get("/api/status")).json()["last_reading_at"] is None
