"""The session anomaly signal in the session check (#461, ADR 0020; ``auth/session_anomaly.py``).

A scripted replay of a session from another network with another browser writes exactly one
``session_anomaly`` event however many requests it makes, the user sees it in their activity, and
the session keeps working (only a signal: nothing is signed out). A normal session writes none.
The one WARNING line carries the signal codes and ids only: never the token, the cookie, the
address (in full or in part) or the agent string. Synthetic accounts, documentation-range
addresses and planted agent strings only.
"""

import json
import logging
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    session_cookie,
)
from ownvitals.auth.cookies import SESSION_COOKIE
from ownvitals.auth.session_anomaly import LOG_MESSAGE, country_of, logger
from ownvitals.auth.session_anomaly_policy import RATE_LIMIT
from ownvitals.log import JsonFormatter

HERE = "127.0.0.1"  # the test client's address
ISSUED_TO = "198.51.100.231"  # where the session was signed in: another network
SCRIPT = "planted-replay-agent/1.0 (Windows NT 10.0) Firefox/140.0 " + "R" * 24


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.handler.setFormatter(JsonFormatter())
    caplog.set_level(logging.DEBUG)
    return caplog


async def _get(env: Env, token: str, agent: str | None = None) -> httpx.Response:
    headers = session_cookie(token)
    if agent is not None:
        headers["User-Agent"] = agent
    return await env.client.get("/api/auth/session", headers=headers)


async def _anomalies(env: Env) -> list[Any]:
    async with env.db() as db:
        rows = await db.execute(
            text("SELECT user_id, ip, detail FROM auth_events WHERE kind = 'session_anomaly'")
        )
        return list(rows.all())


async def test_a_scripted_replay_writes_exactly_one_event_and_signs_nobody_out(
    env: Env, logs: pytest.LogCaptureFixture
) -> None:
    user_id = await env.user()
    issued = await env.full_login(user_id, ip=ISSUED_TO)  # stored agent: "pytest"
    for _ in range(12):
        response = await _get(env, issued.token, SCRIPT)
        assert response.status_code == 200
        assert response.json()["authenticated"] is True  # still signed in
    rows = await _anomalies(env)
    assert [(r.user_id, r.ip, r.detail) for r in rows] == [
        (user_id, HERE, {"signals": ["new_ip_and_agent"]})
    ]
    assert await env.scalar(
        "SELECT anomaly_at IS NOT NULL FROM sessions WHERE id = :i", i=issued.id
    )
    assert await env.scalar("SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id) == 1
    # The user sees it in "Recent security activity".
    events = (
        await env.client.get("/api/auth/activity", headers=session_cookie(issued.token))
    ).json()
    assert [e["kind"] for e in events["events"]] == ["session_anomaly"]
    # One WARNING line, after the commit, with codes and ids only.
    lines = [r for r in logs.records if r.name == logger.name]
    assert [(r.levelno, r.getMessage()) for r in lines] == [(logging.WARNING, LOG_MESSAGE)]
    (line,) = [json.loads(logs.handler.format(r)) for r in lines]
    assert line["signals"] == ["new_ip_and_agent"]
    assert line["user_id"] == str(user_id)
    assert line["session_id"] == str(issued.id)


async def test_the_log_line_holds_no_token_cookie_address_or_agent(
    env: Env, logs: pytest.LogCaptureFixture
) -> None:
    issued = await env.full_login(await env.user(), ip=ISSUED_TO)
    response = await _get(env, issued.token, SCRIPT)
    assert response.status_code == 200
    (record,) = [r for r in logs.records if r.name == logger.name]
    line = logs.handler.format(record)
    for planted in (
        issued.token,
        SESSION_COOKIE,
        "198.51.100",  # not even in part
        "127.0.0",
        "R" * 24,
        "planted-replay-agent",
    ):
        assert planted not in line
    assert set(json.loads(line)) >= {"signals", "user_id", "session_id"}


async def test_a_normal_session_writes_nothing(env: Env, logs: pytest.LogCaptureFixture) -> None:
    issued = await env.full_login(await env.user(), ip=HERE)  # signed in from here
    for _ in range(20):
        assert (await _get(env, issued.token)).status_code == 200
    assert await _anomalies(env) == []
    assert await env.scalar("SELECT seen_count FROM sessions WHERE id = :i", i=issued.id) == 20
    assert [r for r in logs.records if r.name == logger.name] == []


async def test_a_new_network_alone_or_a_new_agent_alone_writes_nothing(env: Env) -> None:
    roaming = await env.full_login(await env.user(), ip=ISSUED_TO)
    assert (await _get(env, roaming.token)).status_code == 200  # same agent family ("other")
    updated = await env.full_login(await env.user("kim@example.com"), ip=HERE)
    assert (await _get(env, updated.token, SCRIPT)).status_code == 200  # same address
    assert await _anomalies(env) == []


async def test_an_impossible_rate_writes_one_event(env: Env) -> None:
    issued = await env.full_login(await env.user(), ip=HERE)
    await env.run(
        "UPDATE sessions SET seen_window_at = now(), seen_count = :n WHERE id = :i",
        n=RATE_LIMIT - 1,
        i=issued.id,
    )
    assert (await _get(env, issued.token)).status_code == 200  # the limit itself: no signal
    assert await _anomalies(env) == []
    for _ in range(3):
        assert (await _get(env, issued.token)).status_code == 200
    rows = await _anomalies(env)
    assert [r.detail for r in rows] == [{"signals": ["request_rate"]}]


async def test_a_new_session_row_starts_a_fresh_history(env: Env) -> None:
    """A new session row (a sign-in, or a rotation after a proof) has no anomaly state."""
    issued = await env.full_login(await env.user(), ip=ISSUED_TO)
    assert (await _get(env, issued.token, SCRIPT)).status_code == 200
    fresh = await env.full_login(await env.user("sam@example.com"), ip=ISSUED_TO)
    row = await env.row(
        "SELECT seen_window_at, seen_count, seen_country, anomaly_at FROM sessions WHERE id = :i",
        i=fresh.id,
    )
    assert tuple(row) == (None, 0, None, None)


async def test_an_anonymous_or_unknown_cookie_is_not_scored(env: Env) -> None:
    assert (await env.client.get("/api/auth/session")).status_code == 200
    assert (await _get(env, "not-a-session-token", SCRIPT)).status_code == 200
    assert await _anomalies(env) == []


@pytest.mark.parametrize(
    ("place", "country"),
    [
        ("Malmö, SE", "SE"),
        ("SE", "SE"),
        ("Somewhere, With, Commas, BR", "BR"),
        ("T1", "T1"),
        (None, None),
        ("Place", None),
        ("se", None),
    ],
)
def test_the_country_is_the_places_code(place: str | None, country: str | None) -> None:
    assert country_of(place) == country
