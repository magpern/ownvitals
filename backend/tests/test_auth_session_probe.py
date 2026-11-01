"""The jobs event stream's session check (#10 replaces #9's seam): ``session_probe`` resolves the
session cookie the stream was opened with, again on each probe, and is true only while that
session is valid and belongs to the stream's user."""

from types import SimpleNamespace
from typing import Any

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401 - fixtures
from flows import Browser, only_user_id, signed_in
from ownvitals.auth.cookies import SESSION_COOKIE
from ownvitals.auth.deps import session_probe


def request_of(env: Env, browser: Browser) -> Any:
    return SimpleNamespace(app=env.app, cookies=dict(browser.cookies))


async def test_the_probe_holds_while_the_session_is_valid_and_ends_with_it(env: Env) -> None:
    browser = await signed_in(env, "me@example.com")
    user = await only_user_id(env)
    probe = session_probe(request_of(env, browser))(user)
    assert await probe() is True
    assert await probe() is True
    await env.run("DELETE FROM sessions")  # revoked (sign out everywhere, admin, purge)
    assert await probe() is False


async def test_the_probe_is_false_for_another_user_or_no_cookie(env: Env) -> None:
    browser = await signed_in(env, "me@example.com")
    other = await env.user("other@example.com")
    assert await session_probe(request_of(env, browser))(other)() is False
    assert await session_probe(request_of(env, Browser(env)))(other)() is False


async def test_an_expired_session_ends_the_probe(env: Env) -> None:
    browser = await signed_in(env, "me@example.com")
    user = await only_user_id(env)
    probe = session_probe(request_of(env, browser))(user)
    await env.run("UPDATE sessions SET expires_at = now() - interval '1 second'")
    assert await probe() is False


async def test_the_probe_keeps_the_cookie_it_was_opened_with(env: Env) -> None:
    """A session rotated away (signing in again) ends the stream: the probe does not follow the
    browser to its new cookie."""
    browser = await signed_in(env, "me@example.com")
    user = await only_user_id(env)
    probe = session_probe(request_of(env, browser))(user)
    old = browser.cookies[SESSION_COOKIE]
    await browser.sign_in("me@example.com")
    assert browser.cookies[SESSION_COOKIE] != old
    assert await probe() is False
