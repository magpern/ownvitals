"""``GET /api/days/{key}/{day}`` and ``GET /api/readings/{id}`` (issue #189): everything recorded
on a day and a reading's drawer (raw value x 10^unit, attrib, device, zone, quality gates,
provenance). Another user's id, day or metric answers 404; no response and no log line carries a
Withings identifier or a token. Synthetic Withings groups through the real passes.
"""

import json
import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from authtools import Env, auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from flows import Browser
from viewtools import (
    DEVICE,
    GRPID_BASE,
    TODAY,
    TOKEN,
    WITHINGS_USER,
    _conn,
    connected_with_streams,
    env_fixture,  # noqa: F401 - fixture
    epoch,
    group,
    ingest,
    plant_device,
    run,
    user,
)

SYNTHETIC = True
DAY = TODAY - timedelta(days=3)
MORNING = GRPID_BASE + 500
AFTERNOON = GRPID_BASE + 501
UNCONFIRMED = GRPID_BASE + 502
PULSE = GRPID_BASE + 503


async def recorded(env: Env, email: str) -> tuple[Browser, Any, Any]:
    browser, owner = await user(env, email)
    connection = connected_with_streams(owner)
    plant_device(owner, connection)
    extra = [
        group(MORNING, epoch(DAY), (1, 80_050, -3, None)),
        group(AFTERNOON, epoch(DAY, 13, 30), (1, 81_000, -3, None)),
        group(UNCONFIRMED, epoch(DAY, 15, 0), (1, 55_000, -3, None), attrib=1),
        group(PULSE, epoch(DAY, 6, 31), (11, 61, 0, None)),
    ]
    await ingest(env, owner, connection, extra=extra)
    return browser, owner, connection


async def day(browser: Browser, key: str = "weight", when: Any = DAY) -> dict[str, Any]:
    response = await browser.get(f"/api/days/{key}/{when.isoformat()}")
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_day_shows_everything_recorded(env: Env) -> None:
    browser, _, _ = await recorded(env, "a@example.com")
    body = await day(browser)
    assert (body["key"], body["day"], body["unit"]) == ("weight", DAY.isoformat(), "kg")
    daily = body["daily"]
    assert (daily["value"], daily["method"], daily["n"]) == (80.05, "morning", 2)
    assert (daily["minimum"], daily["maximum"]) == (80.05, 81.0)
    assert daily["algo_version"] == "daily/1"
    readings = body["readings"]
    assert [r["value"] for r in readings] == [80.05, 81.0, 55.0]
    assert [r["used"] for r in readings] == [True, True, False]
    assert readings[2]["quality_flags"] == ["unconfirmed"]
    assert readings[0]["source_tz"] == "Europe/Stockholm"
    assert readings[0]["local_time"] in {"07:30:00", "08:30:00"}
    assert body["also_recorded"] == [{"metric_key": "heart_pulse", "n_readings": 1}]
    pulse = await day(browser, "heart_pulse")
    assert pulse["daily"]["value"] == 61.0
    assert pulse["also_recorded"][0]["metric_key"] == "weight"


async def test_the_reading_drawer_shows_raw_value_attrib_device_gates_and_provenance(
    env: Env,
) -> None:
    browser, _, _ = await recorded(env, "b@example.com")
    first, _, unconfirmed = (await day(browser))["readings"]
    response = await browser.get(f"/api/readings/{first['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metric_key"] == "weight"
    assert body["unit"] == "kg"
    assert body["value"] == 80.05
    assert body["value_exact"] == "80.050"
    assert body["raw"] == {"value_raw": 80050, "unit_exponent": -3, "conversion": "e-3:kg"}
    assert (body["attrib"], body["attrib_key"], body["category"]) == (0, "reading.attrib.0", 1)
    assert body["device"] == {"model_id": 5, "type": "scale", "firmware": "synthetic-fw-1"}
    assert body["source_tz"] == "Europe/Stockholm"
    assert body["local_day"] == DAY.isoformat()
    assert body["quality_flags"] == []
    assert body["used_in_daily"] is True
    assert {g["gate"]: g["passed"] for g in body["gates"]} == {
        "confirmed": True,
        "plausible": True,
        "not_duplicate": True,
        "no_jump": True,
    }
    provenance = body["provenance"]
    assert provenance["algo_version"] == "normalize/1"
    assert provenance["input_revision"] >= 1
    assert len(provenance["input_fingerprint"]) == 64
    assert provenance["recompute_pending"] is False
    flagged = (await browser.get(f"/api/readings/{unconfirmed['id']}")).json()
    assert flagged["attrib"] == 1
    assert flagged["quality_flags"] == ["unconfirmed"]
    assert flagged["used_in_daily"] is False
    assert {g["gate"]: g["passed"] for g in flagged["gates"]}["confirmed"] is False


async def test_a_jump_is_advisory_and_the_reading_stays_used(env: Env) -> None:
    browser, owner = await user(env, "c@example.com")
    connection = connected_with_streams(owner)
    extra = [
        group(GRPID_BASE + 600, epoch(DAY, 6, 0), (1, 80_000, -3, None)),
        group(GRPID_BASE + 601, epoch(DAY, 12, 0), (1, 84_000, -3, None)),
    ]
    await ingest(env, owner, connection, extra=extra)
    jumped = (await day(browser))["readings"][1]
    body = (await browser.get(f"/api/readings/{jumped['id']}")).json()
    gates = {g["gate"]: g for g in body["gates"]}
    assert gates["no_jump"] == {"gate": "no_jump", "passed": False, "advisory": True}
    assert body["quality_flags"] == ["jump"]
    assert body["used_in_daily"] is True


async def test_another_users_metric_day_and_reading_answer_404(env: Env) -> None:
    alice, _, _ = await recorded(env, "alice@example.com")
    bob, _ = await user(env, "bob@example.com")
    reading_id = (await day(alice))["readings"][0]["id"]
    assert (await alice.get(f"/api/readings/{reading_id}")).status_code == 200
    assert (await bob.get(f"/api/readings/{reading_id}")).status_code == 404
    assert (await bob.get(f"/api/days/weight/{DAY.isoformat()}")).status_code == 404
    assert (await bob.get(f"/api/days/heart_pulse/{DAY.isoformat()}")).status_code == 404


async def test_missing_and_malformed_requests(env: Env) -> None:
    browser, _, _ = await recorded(env, "d@example.com")
    other_day = (TODAY - timedelta(days=9)).isoformat()
    for path in (
        f"/api/readings/{uuid4()}",
        f"/api/days/weight/{other_day}",  # nothing recorded that day
        f"/api/days/no_such_metric/{DAY.isoformat()}",
        "/api/days/weight/0001-01-01",
        "/api/days/weight/9999-12-31",
        f"/api/days/segmental_muscle_mass:left_arm/{DAY.isoformat()}",
    ):
        assert (await browser.get(path)).status_code == 404, path
    for path in (
        "/api/readings/not-a-uuid",
        "/api/days/weight/not-a-date",
        "/api/days/weight/2026-13-40",
    ):
        assert (await browser.get(path)).status_code == 422, path


async def test_a_sleep_observation_is_not_a_browsable_reading(env: Env) -> None:
    browser, owner, _ = await recorded(env, "e@example.com")
    run(
        "INSERT INTO observations (owner_id, metric_key, ts, source_ts, source_tz, value, "
        "source_value_raw, source_unit, conversion, algo_version, input_revision, "
        "input_fingerprint) SELECT owner_id, 'sleep_synthetic', ts, source_ts, source_tz, 1, 1, 0, "
        "'e0:unknown', 'normalize/1', 1, input_fingerprint FROM observations "
        "WHERE owner_id = :o LIMIT 1",
        o=owner,
    )
    with _conn() as engine, engine.begin() as conn:
        sleep_id = conn.execute(
            text(
                "SELECT id FROM observations WHERE owner_id = :o AND metric_key = 'sleep_synthetic'"
            ),
            {"o": owner},
        ).scalar_one()
    assert (await browser.get(f"/api/readings/{sleep_id}")).status_code == 404
    assert any(r["metric_key"] == "sleep_synthetic" for r in (await day(browser))["also_recorded"])


FORBIDDEN_KEYS = ("grpid", "deviceid", "hash_deviceid", "withings_userid", "connection_id", "token")


def keys_of(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys_of(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys_of(v)}
    return set()


async def test_responses_and_logs_carry_no_withings_ids_or_tokens(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    browser, _, connection = await recorded(env, "f@example.com")
    first = (await day(browser))["readings"][0]["id"]
    texts: list[str] = []
    for path in (
        "/api/status",
        "/api/metrics",
        "/api/metrics/weight?range=all",
        f"/api/days/weight/{DAY.isoformat()}",
        f"/api/readings/{first}",
        "/api/nerve/summary",
    ):
        response = await browser.get(path)
        assert response.status_code == 200, path
        assert not keys_of(response.json()) & set(FORBIDDEN_KEYS), path
        texts.append(response.text)
    planted = [
        str(WITHINGS_USER),
        DEVICE,
        TOKEN.decode(),
        str(connection),
        *(str(g) for g in (MORNING, AFTERNOON, UNCONFIRMED, PULSE)),
        str(GRPID_BASE),
    ]
    for secret in planted:
        assert not any(secret in body for body in texts), secret
        assert secret not in caplog.text, secret
    assert json.loads(texts[0])["connection"] == "connected"
