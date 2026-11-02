"""Connect and disconnect (ADR 0004 sections 5, 7 and 9). Synthetic tokens only.

The test provider is the only one the test environment accepts. Connect and disconnect need
recent authentication and a step-up. A stored row's ciphertext does not contain the token,
and the token is not in the response or the log.
"""

import logging
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text

from authtools import (  # noqa: F401
    TEST_TOTP_SECRET,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    running_env,
)
from ctx import make_context
from flows import PASSWORD, Browser, only_user_id, reason, signed_in
from ownvitals.auth import totp
from ownvitals.auth.deps import issue_context
from ownvitals.auth.owners import ACCOUNTS
from ownvitals.connections.repository import (
    connect,
    disconnect,
    list_connections,
    open_credentials,
)
from ownvitals.connections.tables import connections
from ownvitals.crypto.envelope import (
    DecryptFailed,
    connection_aad,
    connection_wrap_aad,
    open_sealed,
)
from ownvitals.db import user_session

ACCESS = "synthetic-access-token-aaaaaaaa"
REFRESH = "synthetic-refresh-token-bbbbbbb"
BODY = {"access_token": ACCESS, "refresh_token": REFRESH}


async def _stepup(env: Env, browser: Browser) -> None:
    await env.run("UPDATE mfa_totp SET last_step = 0")
    code = totp.code_for_step(TEST_TOTP_SECRET, int(totp.clock() // totp.PERIOD))
    response = await browser.post("/api/auth/mfa/stepup", {"code": code})
    assert response.status_code == 200, response.text


async def test_connect_and_disconnect_require_step_up(env: Env) -> None:
    browser = await signed_in(env)

    async def connect_call(active: Browser) -> httpx.Response:
        return await active.post("/api/connections/test", BODY)

    async def disconnect_call(active: Browser) -> httpx.Response:
        return await active.request("DELETE", "/api/connections/test")

    await env.run("UPDATE sessions SET reauth_at = now() - interval '11 minutes'")
    assert reason(await connect_call(browser)) == "reauth_required"
    reauth = await browser.post("/api/auth/reauth", {"password": PASSWORD})
    assert reauth.status_code == 200, reauth.text
    await env.run("UPDATE sessions SET stepup_at = now() - interval '11 minutes'")
    assert reason(await connect_call(browser)) == "step_up_required"
    await _stepup(env, browser)
    created = await connect_call(browser)
    assert created.status_code == 200, created.text
    assert ACCESS not in created.text
    assert REFRESH not in created.text

    await env.run("UPDATE sessions SET stepup_at = now() - interval '11 minutes'")
    assert reason(await disconnect_call(browser)) == "step_up_required"
    await _stepup(env, browser)
    removed = await disconnect_call(browser)
    assert removed.status_code == 204, removed.text
    listed = await browser.get("/api/connections")
    assert listed.status_code == 200, listed.text
    assert listed.json()["connections"][0]["status"] == "disconnected"
    assert ACCESS not in listed.text


async def test_an_unknown_provider_is_refused(env: Env) -> None:
    browser = await signed_in(env)
    withings = await browser.post("/api/connections/withings", BODY)
    assert (withings.status_code, reason(withings)) == (404, "unknown_provider")
    assert ACCESS not in withings.text
    odd = await browser.post("/api/connections/not-a-provider", BODY)
    assert reason(odd) == "unknown_provider"


async def test_the_test_provider_is_unreachable_outside_the_test_environment(
    app_database_url: str, clean_auth: None
) -> None:
    async with running_env(app_database_url, environment="dev") as env:
        browser = await signed_in(env)
        response = await browser.post("/api/connections/test", BODY)
        assert (response.status_code, reason(response)) == (404, "unknown_provider")


async def test_a_bad_body_is_refused_without_echoing_it(env: Env) -> None:
    browser = await signed_in(env)
    response = await browser.post("/api/connections/test", {"access_token": ACCESS})
    assert reason(response) == "invalid_body"
    assert ACCESS not in response.text


async def test_credentials_are_not_stored_or_logged_and_a_wrong_aad_fails(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    browser = await signed_in(env)
    user_id = await only_user_id(env)
    created = await browser.post("/api/connections/test", BODY)
    assert created.status_code == 200, created.text
    connection_id = UUID(created.json()["id"])
    other = await env.user("other@example.com")

    async with user_session(env.factory, make_context(user_id)) as db:
        stored = (
            await db.execute(
                text(
                    "SELECT encode(secret, 'escape') || encode(wrapped_dek, 'escape') "
                    "|| public::text FROM connections WHERE id = :id"
                ),
                {"id": connection_id},
            )
        ).scalar_one()
        row = (
            await db.execute(
                select(
                    connections.c.secret,
                    connections.c.secret_nonce,
                    connections.c.wrapped_dek,
                    connections.c.dek_nonce,
                    connections.c.key_id,
                ).where(connections.c.id == connection_id)
            )
        ).one()
    assert ACCESS not in stored
    assert REFRESH not in stored
    assert ACCESS not in caplog.text
    assert REFRESH not in caplog.text

    opened = await open_credentials(
        env.factory, env.app.state.keyring, make_context(user_id), connection_id
    )
    assert opened.credentials.access_token == ACCESS
    assert ACCESS not in repr(opened)
    assert ACCESS not in repr(opened.credentials)
    assert ACCESS not in str(opened.credentials)

    ring = env.app.state.keyring
    async with user_session(env.factory, make_context(user_id)) as db:
        kek = await ring.kek_for(db, row.key_id)
    with pytest.raises(DecryptFailed):
        open_sealed(
            row.secret,
            row.secret_nonce,
            row.wrapped_dek,
            row.dek_nonce,
            kek,
            connection_aad(other, "test", connection_id),
            connection_wrap_aad(kek.key_id, other, "test", connection_id),
        )

    refs = await ACCOUNTS.connection_refs(env.factory, make_context(user_id))
    assert refs == frozenset({("test", connection_id)})
    issued = await issue_context(env.factory, user_id, "user")
    assert issued.connection("test") == connection_id
    assert await ACCOUNTS.connection_refs(env.factory, make_context(other)) == frozenset()
    async with user_session(env.factory, make_context(other)) as db:
        visible = (await db.execute(select(connections.c.id))).all()
    assert visible == []


async def test_disconnect_keeps_the_row_and_a_new_connection_is_another_row(env: Env) -> None:
    user_id = await env.user("again@example.com")
    ctx = make_context(user_id)
    ring = env.app.state.keyring
    first = await connect(env.factory, ring, ctx, "test", ACCESS, REFRESH)
    assert await disconnect(env.factory, ctx, "test") is True
    second = await connect(env.factory, ring, ctx, "test", ACCESS, REFRESH)
    assert second.id != first.id
    rows = await list_connections(env.factory, ctx)
    assert [row.status for row in rows] == ["disconnected", "connected"]
    assert await ACCOUNTS.connection_refs(env.factory, ctx) == frozenset({("test", second.id)})
