"""The Google re-authentication's user-row lock, held against a real concurrent writer (#282, from
the #261 and #262 reviews; ADR 0003 section 1, amendment of 2026-11-08).

``_reauth`` reads ``has_password`` under ``FOR NO KEY UPDATE`` first, then the identity. So a
password set, or an identity unlinked, while the callback is in flight is seen; and the lock still
lets a foreign-key check (``FOR KEY SHARE``) through. Synthetic accounts only; Google is the
in-process fake.
"""

import asyncio

from sqlalchemy import text

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    smtp_fixture,
)
from flows import age_reauth
from oauthtools import fake_fixture, location, oenv_fixture, start  # noqa: F401 - fixtures
from oidcfake import FakeProviders
from ownvitals.auth.cookies import SESSION_COOKIE
from ownvitals.auth.events import record_event
from ownvitals.auth.oauth_outcomes import has_password
from racetools import WAIT
from test_auth_oauth_reauth import REAUTHED, failures, google_only, refused_unchanged

SYNTHETIC_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c3ludGhldGlj$c3ludGhldGljLWhhc2g"


async def test_a_password_set_while_the_callback_runs_refuses_the_google_reauth(
    oenv: Env, fake: FakeProviders
) -> None:
    browser, user_id = await google_only(oenv, fake)
    await age_reauth(oenv)
    old = browser.cookies[SESSION_COOKIE]
    started = await start(browser, "google", "reauth")
    callback = fake.complete(started.json()["authorization_url"])
    async with oenv.db() as db:  # a password change in flight: it holds the user row
        await db.execute(
            text("UPDATE users SET password_hash = :h WHERE id = :u"),
            {"h": SYNTHETIC_HASH, "u": user_id},
        )
        answer = asyncio.create_task(browser.get(callback))
        await asyncio.sleep(WAIT)
        assert not answer.done()  # the re-auth waits for the change before it reads
    assert location(await answer) == "/account?oauth=reauth_use_password"
    await refused_unchanged(oenv, browser, old)
    assert await failures(oenv) == [{"why": "has_password", "provider": "google"}]


async def test_an_unlink_while_the_callback_runs_refuses_the_google_reauth(
    oenv: Env, fake: FakeProviders
) -> None:
    browser, user_id = await google_only(oenv, fake)
    await age_reauth(oenv)
    old = browser.cookies[SESSION_COOKIE]
    started = await start(browser, "google", "reauth")
    callback = fake.complete(started.json()["authorization_url"])
    async with oenv.db() as db:  # an unlink in flight: the user row first, then the identity
        await db.execute(
            text("SELECT 1 FROM users WHERE id = :u FOR NO KEY UPDATE"), {"u": user_id}
        )
        await db.execute(text("DELETE FROM identities WHERE user_id = :u"), {"u": user_id})
        answer = asyncio.create_task(browser.get(callback))
        await asyncio.sleep(WAIT)
        assert not answer.done()
    assert location(await answer) == "/account?oauth=identity_mismatch"
    await refused_unchanged(oenv, browser, old)


async def test_the_reauth_lock_lets_a_foreign_key_check_through(
    oenv: Env, fake: FakeProviders
) -> None:
    """``has_password(lock=True)`` takes ``NO KEY UPDATE``: an ``auth_events`` insert for the user
    (``FOR KEY SHARE`` on the user row) is not blocked by it. ``FOR UPDATE`` would block it."""
    _, user_id = await google_only(oenv, fake)
    async with oenv.db() as held:
        assert await has_password(held, user_id, lock=True) is False
        async with oenv.db() as other:
            await asyncio.wait_for(record_event(other, "sign_in", user_id=user_id), 5)
    assert await oenv.scalar("SELECT count(*) FROM auth_events WHERE kind = 'sign_in'") >= 1  # pyright: ignore[reportOperatorIssue]


async def test_an_uncontended_google_reauth_still_goes_through(
    oenv: Env, fake: FakeProviders
) -> None:
    browser, _ = await google_only(oenv, fake)
    await age_reauth(oenv)
    started = await start(browser, "google", "reauth")
    assert location(await browser.get(fake.complete(started.json()["authorization_url"]))) == (
        REAUTHED
    )
