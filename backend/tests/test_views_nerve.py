"""``GET /api/nerve/summary`` (issue #189, ADR 0010 section 8): the monthly summary of the nerve
metrics and of N2 and N3. The metrics come from synthetic groups through the real passes; the
findings come only through ``views.findings_read``, so an experimental one is returned only when
the owner-only switch is effective for the caller. Synthetic data only.
"""

from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    running_env,
)
from flows import Browser
from ownvitals.views.deps import view_now
from test_views_findings import plant_finding, store_flag
from viewtools import (
    NOW,
    TODAY,
    connected_with_streams,
    env_fixture,  # noqa: F401 - fixture
    epoch,
    group,
    ingest,
    user,
)

SYNTHETIC = True
OCT = [date(2026, 10, d) for d in (2, 5, 9, 14, 20, 26)]
SEP = [date(2026, 9, d) for d in (3, 17)]


@pytest.fixture(name="owner_env")
async def owner_env_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    store_flag(None)
    async with running_env(app_database_url, experimental_findings=True) as running:
        running.app.dependency_overrides[view_now] = lambda: NOW
        yield running


async def nerve_user(env: Env, email: str) -> tuple[Browser, Any]:
    browser, owner = await user(env, email)
    connection = connected_with_streams(owner)
    extra = []
    for n, day in enumerate(OCT + SEP):
        base = 800_000 + n * 1000
        extra.append(group(base, epoch(day), (167, 60 + n, 0, None), (196, 50 + n, 0, None)))
    await ingest(env, owner, connection, extra=extra)
    return browser, owner


async def summary(browser: Browser, query: str = "") -> dict[str, Any]:
    response = await browser.get(f"/api/nerve/summary{query}")
    assert response.status_code == 200, response.text
    return response.json()


async def test_months_run_newest_first_with_the_days_median_and_extremes(env: Env) -> None:
    browser, _ = await nerve_user(env, "a@example.com")
    body = await summary(browser)
    assert [m["month"] for m in body["months"]][:4] == ["2026-11", "2026-10", "2026-09", "2026-08"]
    assert len(body["months"]) == 12
    october = {m["metric_key"]: m for m in body["months"][1]["metrics"]}
    health = october["nerve_health_score"]
    assert health["days_with_data"] == 6
    assert (health["minimum"], health["maximum"], health["median"]) == (60.0, 65.0, 62.5)
    assert october["esc"] == {
        "metric_key": "esc",
        "days_with_data": 0,
        "median": None,
        "minimum": None,
        "maximum": None,
    }
    november = body["months"][0]["metrics"]
    assert all(m["days_with_data"] == 0 and m["median"] is None for m in november)
    assert (await summary(browser, "?months=3"))["months"][-1]["month"] == "2026-09"
    for bad in ("?months=0", "?months=37"):
        assert (await browser.get(f"/api/nerve/summary{bad}")).status_code == 422


async def test_the_metrics_carry_the_nerve_minimum_data_state(env: Env) -> None:
    browser, _ = await nerve_user(env, "b@example.com")
    by_key = {m["key"]: m for m in (await summary(browser))["metrics"]}
    # N1: at least 4 days with data over at least 28 days elapsed
    assert by_key["nerve_health_score"]["state"] == "up_to_date"
    assert by_key["nerve_health_score"]["latest"]["day"] == "2026-10-26"
    assert by_key["nerve_health_score"]["progress"] is None
    assert by_key["esc"]["state"] == "insufficient_data"
    assert by_key["esc"]["latest"] is None
    progress = by_key["esc"]["progress"]
    assert (progress["days_with_data"], progress["days_required"]) == (0, 4)
    assert progress["span_required"] == 28


async def test_a_user_with_too_little_data_gets_the_progress_not_a_value(env: Env) -> None:
    browser, owner = await user(env, "c@example.com")
    connection = connected_with_streams(owner)
    extra = [group(810_000 + n, epoch(d), (167, 61, 0, None)) for n, d in enumerate(SEP)]
    await ingest(env, owner, connection, extra=extra)
    health = {m["key"]: m for m in (await summary(browser))["metrics"]}["nerve_health_score"]
    assert health["state"] == "insufficient_data"
    assert (health["progress"]["days_with_data"], health["progress"]["span_days"]) == (2, 14)
    assert health["progress"]["shortfalls"] == ["days_with_data", "span_days"]


async def test_n2_and_n3_findings_come_in_their_month_and_nothing_else(env: Env) -> None:
    browser, owner = await nerve_user(env, "d@example.com")
    n2 = plant_finding(owner, "N2", metric_keys=("nerve_health_score",))
    n3 = plant_finding(owner, "N3", metric_keys=("nerve_response_score",))
    plant_finding(owner, "N5", metric_keys=("nerve_foot_158",))  # not N2 or N3
    plant_finding(owner, "C1", metric_keys=("heart_pulse",))  # not nerve
    body = await summary(browser)
    september = next(m for m in body["months"] if m["month"] == "2026-09")
    assert {f["id"] for f in september["findings"]} == {str(n2), str(n3)}
    assert {f["rule_id"] for f in september["findings"]} == {"N2", "N3"}
    assert all(m["findings"] == [] for m in body["months"] if m["month"] != "2026-09")
    assert september["findings"][0]["why_key"].startswith("finding.N")


async def experimental_ids(running: Env, email: str) -> tuple[set[str], Any, Any]:
    browser, owner = await nerve_user(running, email)
    plain = plant_finding(owner, "N2", metric_keys=("nerve_health_score",))
    experimental = plant_finding(
        owner, "N3", tier="experimental", metric_keys=("nerve_health_score",)
    )
    body = await summary(browser)
    september = next(m for m in body["months"] if m["month"] == "2026-09")
    return {f["id"] for f in september["findings"]}, plain, experimental


async def test_an_experimental_finding_is_not_returned_without_the_switch(env: Env) -> None:
    ids, plain, experimental = await experimental_ids(env, "e@example.com")
    assert str(plain) in ids
    assert str(experimental) not in ids


async def test_an_experimental_finding_is_returned_behind_the_switch(owner_env: Env) -> None:
    ids, plain, experimental = await experimental_ids(owner_env, "f@example.com")
    assert {str(plain), str(experimental)} <= ids


async def test_another_users_summary_is_empty(env: Env) -> None:
    await nerve_user(env, "alice@example.com")
    _, bob_id = await user(env, "bob@example.com")
    plant_finding(bob_id, "N2", metric_keys=("nerve_health_score",))
    other, _ = await user(env, "carol@example.com")
    body = await summary(other)
    assert all(m["days_with_data"] == 0 for mo in body["months"] for m in mo["metrics"])
    assert all(mo["findings"] == [] for mo in body["months"])
    assert body["metrics"][0]["latest"] is None
    assert TODAY.year == 2026
