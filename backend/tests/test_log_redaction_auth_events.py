"""The new ``auth_events`` kinds of #251 never put a secret or an identifier in a log line.

Each path runs with the production ``JsonFormatter`` at DEBUG, with synthetic values planted
where a line could carry them: the session token, the agent string, the email and the address.
``sign_out``, ``session_signed_out``, ``logout_everywhere``, a failed event write and the
activity read are covered (the redaction tests of ``test_log_redaction.py`` cover the
formatter itself).
"""

import logging
from typing import Any

import pytest
from sqlalchemy import insert

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    session_cookie,
    unsafe_headers,
)
from ownvitals.auth import routes
from ownvitals.auth.sessions import IssuedSession
from ownvitals.auth.tables import auth_events
from ownvitals.log import QUIET_LOGGERS, JsonFormatter

AGENT = "Mozilla/5.0 (X11; Linux x86_64) Firefox/140.0 planted-agent-" + "A" * 24
EMAIL = "planted-log-user@example.com"
ADDRESS = "198.51.100.231"


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.handler.setFormatter(JsonFormatter())
    caplog.set_level(logging.DEBUG)
    for name in QUIET_LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name)
    return caplog


async def _headers(env: Env, issued: IssuedSession) -> dict[str, str]:
    session = await env.client.get("/api/auth/session", headers=session_cookie(issued.token))
    return {
        **unsafe_headers(issued.token, session.json()["csrf_token"]),
        "User-Agent": AGENT,
        "X-Forwarded-For": ADDRESS,
    }


def _assert_clean(text: str, *issued: IssuedSession) -> None:
    for value in (AGENT, "A" * 24, EMAIL, "planted-log-user", ADDRESS):
        assert value not in text
    for session in issued:  # the id is in the access log's path of DELETE /sessions/{id}
        assert session.token not in text


async def test_the_sign_out_kinds_log_nothing_planted(
    env: Env, logs: pytest.LogCaptureFixture
) -> None:
    user_id = await env.user(EMAIL)
    mine = await env.full_login(user_id, stepup=True, ip=ADDRESS)
    other = await env.login(user_id, ip=ADDRESS)
    third = await env.login(user_id, ip=ADDRESS)
    headers = await _headers(env, mine)
    assert (
        await env.client.delete(f"/api/auth/sessions/{other.id}", headers=headers)
    ).status_code == 204
    activity = await env.client.get("/api/auth/activity", headers=headers)
    assert activity.status_code == 200
    assert (await env.client.post("/api/auth/logout-all", headers=headers)).status_code == 204
    signed_out = await env.full_login(user_id, ip=ADDRESS)
    headers = await _headers(env, signed_out)
    assert (await env.client.post("/api/auth/logout", headers=headers)).status_code == 204
    assert (
        await env.scalar(
            "SELECT count(DISTINCT kind) FROM auth_events "
            "WHERE kind IN ('sign_out', 'session_signed_out', 'logout_everywhere')"
        )
        == 3
    )
    _assert_clean(logs.text, mine, other, third, signed_out)


async def test_a_failed_sign_out_event_logs_the_class_only(
    env: Env, monkeypatch: pytest.MonkeyPatch, logs: pytest.LogCaptureFixture
) -> None:
    async def broken(db: Any, kind: str, **_: Any) -> None:
        # A check violation whose message would carry the planted value and the row.
        await db.execute(insert(auth_events).values(kind=kind, browser=AGENT, place=EMAIL * 2))

    monkeypatch.setattr(routes, "record_event", broken)
    issued = await env.login(await env.user(EMAIL), ip=ADDRESS)
    response = await env.client.post("/api/auth/logout", headers=await _headers(env, issued))
    assert response.status_code == 204
    assert "sign-out event write failed: IntegrityError (SQLSTATE 23514)" in logs.text
    _assert_clean(logs.text, issued)
