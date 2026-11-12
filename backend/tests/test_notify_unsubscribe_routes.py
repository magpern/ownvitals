"""The one-click unsubscribe endpoint and the settings' email preference API (ADR 0019; #459),
through the real app (its lifespan, the app role, the auth middleware). Synthetic accounts only.

- ``GET /api/unsubscribe`` never changes state, whatever the token (``test_get_*``);
- ``POST`` needs no session, Origin or CSRF token (RFC 8058), changes exactly the one preference
  row its token names, in the web context (the app role, no user), and answers the same for a
  valid, tampered, expired, other user's, other kind's and replayed token (``test_post_*``);
- the endpoint is rate-limited per address (``test_post_is_rate_limited``);
- the token never appears in a log record, from the mail it is sent in to the request that uses
  it (``test_the_token_is_never_logged``);
- ``/api/notify/email`` shows and edits the signed-in user's own preferences, and a change there
  voids a link sent before it (``test_the_settings_*``).
"""

import base64
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from authtools import (  # noqa: F401 - fixtures
    ORIGIN,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from flows import signed_in
from jobtools import admin_url, engine_for
from ownvitals.auth.ratelimit import UNSUBSCRIBE_IP
from ownvitals.notify import unsubscribe
from ownvitals.notify.email_policy import SUPPRESSIBLE_KINDS
from windowtools import clear_of_window

PATH = "/api/unsubscribe"
Prefs = set[tuple[uuid.UUID, str, bool, int]]


async def _prefs(env: Env) -> Prefs:
    """Every preference row, as the superuser would see it (the app role sees none here)."""
    with engine_for(admin_url()) as engine, engine.connect() as connection:
        rows = connection.execute(
            text("SELECT owner_id, kind, enabled, generation FROM email_prefs")
        ).all()
    return {(r.owner_id, str(r.kind), bool(r.enabled), int(r.generation)) for r in rows}


def _plant(sql: str, **params: object) -> None:
    """As the superuser: the app role cannot write another user's preference, by design."""
    with engine_for(admin_url()) as engine, engine.begin() as connection:
        connection.execute(text(sql), params)


def _token(
    env: Env,
    owner: uuid.UUID,
    kind: str = "findings_waiting",
    generation: int = 0,
    *,
    now: datetime | None = None,
) -> str:
    key = env.app.state.unsubscribe_key
    return unsubscribe.issue(key, owner, kind, generation, now or datetime.now(UTC))


async def _post(env: Env, token: str, **headers: str) -> tuple[int, str]:
    env.client.cookies.clear()
    response = await env.client.post(
        f"{PATH}?t={token}",
        content=b"List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded", **headers},
    )
    return response.status_code, response.text


async def test_get_shows_the_confirm_page_and_changes_nothing(env: Env) -> None:
    alice = await env.user("synthetic-unsub-a@example.test")
    token = _token(env, alice)
    for value in (token, "forged", ""):
        response = await env.client.get(f"{PATH}?t={value}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"] == "no-store"
        assert 'method="post"' in response.text
        assert "Stop these emails?" in response.text
    assert f'action="?t={token}"' in (await env.client.get(f"{PATH}?t={token}")).text
    assert "forged" not in (await env.client.get(f"{PATH}?t=forged")).text  # not token-shaped
    assert (await env.client.head(f"{PATH}?t={token}")).status_code == 200
    assert await _prefs(env) == set()
    slots = await env.scalar("SELECT count(*) FROM rate_limits")
    assert slots == 0  # a GET takes no rate-limit slot either


async def test_get_answers_in_the_request_s_language(env: Env) -> None:
    response = await env.client.get(f"{PATH}?t=x", headers={"Accept-Language": "sv"})
    assert '<html lang="sv">' in response.text
    assert "Sluta få de här mejlen?" in response.text


async def test_post_without_a_session_origin_or_csrf_switches_exactly_one_row(env: Env) -> None:
    alice = await env.user("synthetic-unsub-b@example.test")
    bob = await env.user("synthetic-unsub-c@example.test")
    _plant(
        "INSERT INTO email_prefs (owner_id, kind, enabled, generation) VALUES "
        "(:b, 'findings_waiting', true, 0), (:a, 'ai_digest_ready', true, 0)",
        a=alice,
        b=bob,
    )
    before = await _prefs(env)
    status, body = await _post(env, _token(env, alice))
    assert status == 200
    assert "Thank you" in body
    after = await _prefs(env)
    assert after - before == {(alice, "findings_waiting", False, 1)}
    assert before - after == set()


async def test_post_answers_the_same_for_every_token_and_changes_nothing_but_the_valid_one(
    env: Env,
) -> None:
    alice = await env.user("synthetic-unsub-d@example.test")
    bob = await env.user("synthetic-unsub-e@example.test")
    valid = _token(env, alice)
    raw = bytearray(base64.urlsafe_b64decode(valid + "="))
    raw[-1] ^= 0x01
    tampered = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()
    old = datetime.now(UTC) - unsubscribe.LIFETIME - timedelta(minutes=1)
    expired = _token(env, alice, now=old)
    foreign_key = unsubscribe.issue(bytes(32), alice, "findings_waiting", 0, datetime.now(UTC))
    answers = set()
    for token in (tampered, expired, foreign_key, "", "x" * 67):
        answers.add(await _post(env, token))
    assert await _prefs(env) == set()
    # Bob's own valid token touches only Bob; the replay of Alice's after hers is the same page.
    answers.add(await _post(env, valid))
    answers.add(await _post(env, valid))  # replay
    answers.add(await _post(env, _token(env, bob, "ai_digest_ready")))
    assert len(answers) == 1, answers  # one status, one body, whatever the token
    assert await _prefs(env) == {
        (alice, "findings_waiting", False, 1),
        (bob, "ai_digest_ready", False, 1),
    }


async def test_post_of_one_kind_leaves_the_other_kinds_on(env: Env) -> None:
    alice = await env.user("synthetic-unsub-f@example.test")
    await _post(env, _token(env, alice, "connection_attention"))
    assert await _prefs(env) == {(alice, "connection_attention", False, 1)}


async def test_post_is_rate_limited(env: Env) -> None:
    await clear_of_window(900)
    alice = await env.user("synthetic-unsub-g@example.test")
    for _ in range(UNSUBSCRIBE_IP.max_count):
        assert (await _post(env, "x" * 67))[0] == 200
    env.client.cookies.clear()
    response = await env.client.post(f"{PATH}?t={_token(env, alice)}")
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    assert await _prefs(env) == set()  # refused before the token is even read


@pytest.mark.parametrize(
    "path",
    [
        "/api/unsubscribe/",
        "/api/unsubscribe-x",
        "/api/unsubscribe/x",
        "/api/unsubscribex",
        "/api/unsubscribe%2F",
        "/API/unsubscribe",
        "/api/Unsubscribe",
        "/api//unsubscribe",
    ],
)
async def test_only_the_exact_path_is_the_endpoint(env: Env, path: str) -> None:
    """A prefix, a trailing slash, another case or an encoded slash is not served by the
    middleware: GET is the app's 404 code, and POST meets the auth middleware's Origin check."""
    alice = await env.user("synthetic-unsub-p@example.test")
    token = _token(env, alice)
    env.client.cookies.clear()
    got = await env.client.get(f"{path}?t={token}")
    assert got.status_code == 404
    assert "text/html" not in got.headers.get("content-type", "")
    posted = await env.client.post(f"{path}?t={token}")
    assert posted.status_code == 403
    assert posted.json()["detail"]["reason"] == "origin_missing"
    assert await _prefs(env) == set()


async def test_other_methods_are_refused(env: Env) -> None:
    for method in ("PUT", "DELETE", "PATCH"):
        response = await env.client.request(method, f"{PATH}?t=x")
        assert response.status_code == 405
        assert response.headers["allow"] == "GET, HEAD, POST"


async def test_the_token_is_never_logged(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    alice = await env.user("synthetic-unsub-h@example.test")
    token = _token(env, alice)
    with caplog.at_level(logging.DEBUG):
        await env.client.get(f"{PATH}?t={token}")
        await _post(env, token)
        await _post(env, token)
    assert caplog.records  # the endpoint does log, just not the token
    for record in caplog.records:
        rendered = f"{record.getMessage()} {record.args!r} {vars(record)!r}"
        assert token not in rendered
        assert str(alice) not in rendered


async def test_the_settings_show_and_edit_the_user_s_own_preferences(env: Env) -> None:
    browser = await signed_in(env, "synthetic-unsub-i@example.test")
    other = await env.user("synthetic-unsub-j@example.test")
    _plant(
        "INSERT INTO email_prefs (owner_id, kind, enabled) VALUES (:o, 'findings_waiting', false)",
        o=other,
    )
    shown = await browser.get("/api/notify/email")
    assert shown.status_code == 200, shown.text
    assert shown.json() == {"kinds": [{"kind": k, "enabled": True} for k in SUPPRESSIBLE_KINDS]}
    off = await browser.request(
        "PUT", "/api/notify/email", {"kind": "ai_digest_ready", "enabled": False}
    )
    assert off.status_code == 200, off.text
    assert {k["kind"]: k["enabled"] for k in off.json()["kinds"]}["ai_digest_ready"] is False
    for refused in (
        {"kind": "export_ready", "enabled": False},
        {"kind": "password_reset", "enabled": False},
        {"kind": "findings_waiting", "enabled": "no"},
        {"kind": "findings_waiting", "enabled": False, "owner_id": str(other)},
    ):
        assert (await browser.request("PUT", "/api/notify/email", refused)).status_code == 422
    rows = await _prefs(env)
    assert (other, "findings_waiting", False, 0) in rows  # the other user's row is untouched


async def test_the_settings_need_a_session(env: Env) -> None:
    env.client.cookies.clear()
    assert (await env.client.get("/api/notify/email")).status_code == 401


async def test_a_change_in_the_settings_voids_a_link_sent_before(env: Env) -> None:
    browser = await signed_in(env, "synthetic-unsub-k@example.test")
    user = uuid.UUID(
        str(
            await env.scalar(
                "SELECT id FROM users WHERE email_normalized = 'synthetic-unsub-k@example.test'"
            )
        )
    )
    sent_before = _token(env, user, "findings_waiting", 0)
    for enabled in (False, True):  # off and on again: two changes
        response = await browser.request(
            "PUT", "/api/notify/email", {"kind": "findings_waiting", "enabled": enabled}
        )
        assert response.status_code == 200
    await _post(env, sent_before)
    assert await _prefs(env) == {(user, "findings_waiting", True, 2)}  # still on
