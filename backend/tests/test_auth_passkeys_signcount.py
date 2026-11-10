"""The sign count (ADR 0011 section 2), with a software authenticator.

A count that does not move forward suspends the credential, writes ``passkey_suspended`` and
mails the account; both counts zero (a synced passkey) is normal. The new count is written by a
conditional update, so two assertions with the same count can't both pass. Synthetic only.
"""

import asyncio

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    menv_fixture,
    smtp_fixture,
)
from flows import Browser, signed_in
from passkeytools import BASE, SoftAuthenticator, failed, reauth, register, sign_in, signin_options
from smtpfake import SmtpFake


async def with_passkey(env: Env, counter_step: int = 1) -> SoftAuthenticator:
    browser = await signed_in(env)
    auth = SoftAuthenticator(counter_step=counter_step)
    assert (await register(browser, auth)).status_code == 201
    await env.run("DELETE FROM sessions")
    return auth


async def count(env: Env) -> int:
    return int(str(await env.scalar("SELECT sign_count FROM webauthn_credentials")))


async def suspended(env: Env) -> bool:
    return bool(await env.scalar("SELECT suspended_at IS NOT NULL FROM webauthn_credentials"))


async def test_a_regression_suspends_audits_and_mails(menv: Env, smtp: SmtpFake) -> None:
    auth = await with_passkey(menv)
    assert (await sign_in(Browser(menv), auth)).status_code == 200  # count 1
    assert (await sign_in(Browser(menv), auth, sign_count=5)).status_code == 200
    assert await count(menv) == 5
    assert failed(await sign_in(Browser(menv), auth, sign_count=3))
    assert await suspended(menv)
    assert await count(menv) == 5
    kinds = await menv.scalar(
        "SELECT array_agg(kind ORDER BY at) FROM auth_events WHERE kind LIKE 'passkey_s%'"
    )
    assert "passkey_suspended" in kinds  # pyright: ignore[reportOperatorIssue]
    [mail] = [m for m in smtp.messages if "suspended" in m.subject]
    assert "Passkey" in mail.text
    # Suspended: even a correct, higher count no longer signs in, and the count stays.
    assert failed(await sign_in(Browser(menv), auth, sign_count=9))
    assert await count(menv) == 5
    assert len([m for m in smtp.messages if "suspended" in m.subject]) == 1


async def test_an_equal_non_zero_count_is_a_regression(env: Env) -> None:
    auth = await with_passkey(env)
    assert (await sign_in(Browser(env), auth, sign_count=4)).status_code == 200
    assert failed(await sign_in(Browser(env), auth, sign_count=4))
    assert await suspended(env)


async def test_a_stored_count_followed_by_zero_suspends(env: Env) -> None:
    auth = await with_passkey(env)
    assert (await sign_in(Browser(env), auth, sign_count=5)).status_code == 200
    assert failed(await sign_in(Browser(env), auth, sign_count=0))
    assert await suspended(env)


async def test_zero_and_zero_is_a_synced_passkey_and_accepted(env: Env) -> None:
    auth = await with_passkey(env, counter_step=0)
    for _ in range(3):
        assert (await sign_in(Browser(env), auth)).status_code == 200
    assert await count(env) == 0
    assert not await suspended(env)


async def test_a_regression_with_a_bad_signature_suspends_nothing(env: Env) -> None:
    """The count is judged only after the signature: a forged low count can't suspend."""
    auth = await with_passkey(env)
    assert (await sign_in(Browser(env), auth, sign_count=5)).status_code == 200
    assert failed(await sign_in(Browser(env), auth, sign_count=1, bad_signature=True))
    assert not await suspended(env)


async def test_two_assertions_with_the_same_count_exactly_one_passes(env: Env) -> None:
    auth = await with_passkey(env)
    first, second = Browser(env), Browser(env)
    one, two = await signin_options(first), await signin_options(second)
    a = auth.get(one, sign_count=7)
    b = auth.get(two, sign_count=7)
    answers = await asyncio.gather(
        first.post(f"{BASE}/signin", {"credential": a}),
        second.post(f"{BASE}/signin", {"credential": b}),
    )
    assert sorted(r.status_code for r in answers) == [200, 401]
    assert await env.scalar("SELECT count(*) FROM sessions") == 1
    assert await count(env) == 7


async def test_a_regression_at_re_auth_mails_a_notice_that_fits_it(
    menv: Env, smtp: SmtpFake
) -> None:
    """The notice is sent from the sign-in and the re-auth alike, so it does not say "a sign-in"
    (#282, from the #267 review; classbeat #797)."""
    browser = await signed_in(menv)
    auth = SoftAuthenticator()
    assert (await register(browser, auth)).status_code == 201
    assert (await reauth(browser, auth, sign_count=5)).status_code == 200
    assert failed(await reauth(browser, auth, sign_count=3))
    assert await suspended(menv)
    [mail] = [m for m in smtp.messages if "suspended" in m.subject]
    assert "A sign-in with" not in mail.text
    assert "to sign in or to confirm it's you" in mail.text
