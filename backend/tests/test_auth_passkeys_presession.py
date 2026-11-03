"""The pre-session cookie (ADR 0011 section 4). Synthetic accounts and keys only."""

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from flows import Browser, signed_in
from ownvitals.auth.cookies import PRE_COOKIE
from ownvitals.auth.keys import token_hash
from passkeytools import BASE, SoftAuthenticator, failed, register


async def test_the_cookie_attributes_and_its_server_state(env: Env) -> None:
    browser = Browser(env)
    answer = await browser.post(f"{BASE}/signin/options")
    [header] = [h for h in answer.headers.get_list("set-cookie") if h.startswith(PRE_COOKIE)]
    attributes = {part.strip().lower() for part in header.split(";")[1:]}
    assert {"httponly", "secure", "samesite=strict", "path=/", "max-age=300"} <= attributes
    assert not any(a.startswith("domain") for a in attributes)
    token = browser.cookies[PRE_COOKIE]
    row = await env.row("SELECT token_hash, expires_at - created_at AS life FROM pre_sessions")
    assert bytes(row.token_hash) == token_hash(token)  # only the hash is stored
    assert row.life.total_seconds() == 300


async def test_every_start_issues_a_new_one_and_drops_the_old(env: Env) -> None:
    browser = Browser(env)
    await browser.post(f"{BASE}/signin/options")
    first = browser.cookies[PRE_COOKIE]
    await browser.post(f"{BASE}/signin/options")
    assert browser.cookies[PRE_COOKIE] != first
    assert await env.scalar("SELECT count(*) FROM pre_sessions") == 1
    assert await env.scalar("SELECT count(*) FROM webauthn_challenges") == 1  # the old one went


async def test_it_is_read_from_its_cookie_only(env: Env) -> None:
    signer = await signed_in(env)
    auth = SoftAuthenticator()
    assert (await register(signer, auth)).status_code == 201
    await env.run("DELETE FROM sessions")
    browser = Browser(env)
    options = (await browser.post(f"{BASE}/signin/options")).json()
    token = browser.cookies.pop(PRE_COOKIE)
    credential = auth.get(options)
    csrf = await browser.csrf()
    for headers, path in (
        ({"X-Ownvitals-Pre": token}, f"{BASE}/signin"),
        ({PRE_COOKIE: token}, f"{BASE}/signin"),
        ({}, f"{BASE}/signin?pre={token}"),
        ({}, f"{BASE}/signin?{PRE_COOKIE}={token}"),
    ):
        env.client.cookies.clear()
        answer = await env.client.post(
            path,
            json={"credential": credential},
            headers={
                "Origin": "https://ownvitals.test",
                "X-CSRF-Token": csrf,
                "Cookie": "; ".join(f"{k}={v}" for k, v in browser.cookies.items()),
                **headers,
            },
        )
        assert failed(answer), (headers, path)
    assert await env.scalar("SELECT count(*) FROM sessions") == 0


async def test_an_expired_pre_session_binds_nothing(env: Env) -> None:
    signer = await signed_in(env)
    auth = SoftAuthenticator()
    assert (await register(signer, auth)).status_code == 201
    await env.run("DELETE FROM sessions")
    browser = Browser(env)
    options = (await browser.post(f"{BASE}/signin/options")).json()
    await env.run("UPDATE pre_sessions SET expires_at = now() - interval '1 second'")
    assert failed(await browser.post(f"{BASE}/signin", {"credential": auth.get(options)}))
