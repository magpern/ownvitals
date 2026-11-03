"""Passkey registration (ADR 0011 sections 1 and 2), with a software authenticator.

Every key is synthetic (``passkeytools``). Each broken part of a registration response is
refused with the one generic answer, and its challenge is spent either way.
"""

import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    menv_fixture,
    smtp_fixture,
)
from flows import Browser, only_user_id, pass_second_factor, reason, signed_in
from passkeytools import (
    AT,
    BASE,
    UP,
    UV,
    SoftAuthenticator,
    b64,
    register,
    register_options,
)
from smtpfake import SmtpFake


async def test_a_registration_stores_the_passkey_and_mails_the_user(
    menv: Env, smtp: SmtpFake
) -> None:
    browser = await signed_in(menv)
    auth = SoftAuthenticator()
    answer = await register(browser, auth, name="  Laptop​  ")
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert body["name"] == "Laptop"  # NFKC, format characters removed, trimmed
    assert body["suspended"] is False
    row = await menv.row(
        "SELECT credential_id, sign_count, transports, aaguid::text, name FROM webauthn_credentials"
    )
    assert bytes(row.credential_id) == auth.credentials[0].credential_id
    assert row.sign_count == 0
    assert sorted(row.transports) == ["hybrid", "internal"]
    assert row.aaguid == "00000000-0000-0000-0000-000000000000"
    assert await menv.scalar("SELECT count(*) FROM webauthn_challenges") == 0  # spent
    kinds = await menv.scalar("SELECT array_agg(kind) FROM auth_events")
    assert "passkey_registered" in kinds  # pyright: ignore[reportOperatorIssue]
    [mail] = [m for m in smtp.messages if "passkey was added" in m.subject]
    assert "Laptop" in mail.text


async def test_the_default_name_and_a_name_too_long(env: Env) -> None:
    browser = await signed_in(env)
    auth = SoftAuthenticator()
    assert (await register(browser, auth)).json()["name"] == "Passkey"
    options = await register_options(browser)
    too_long = await browser.post(
        f"{BASE}/register", {"credential": auth.create(options), "name": "x" * 65}
    )
    assert (too_long.status_code, reason(too_long)) == (422, "name_invalid")


async def test_exclude_credentials_lists_the_existing_passkeys(env: Env) -> None:
    browser = await signed_in(env)
    auth = SoftAuthenticator()
    assert (await register(browser, auth)).status_code == 201
    options = await register_options(browser)
    assert [c["id"] for c in options["excludeCredentials"]] == [
        b64(auth.credentials[0].credential_id)
    ]


BROKEN = {
    "wrong origin": {"origin": "https://evil.test"},
    "a sibling origin": {"origin": "https://app.ownvitals.test"},
    "wrong rp id hash": {"rp_id": "evil.test"},
    "wrong type": {"kind": "webauthn.get"},
    "foreign challenge": {"challenge": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
    "user verification missing": {"flags": UP | AT},
    "user presence missing": {"flags": UV | AT},
    "a disallowed algorithm": {"algorithm": "es384"},
    "made in a cross-origin frame": {"cross_origin": True},
}


@pytest.mark.parametrize("broken", list(BROKEN))
async def test_each_broken_registration_is_refused(env: Env, broken: str) -> None:
    browser = await signed_in(env)
    answer = await register(browser, SoftAuthenticator(), **BROKEN[broken])
    assert (answer.status_code, answer.json()) == (400, {"detail": {"reason": "passkey_failed"}})
    assert await env.scalar("SELECT count(*) FROM webauthn_credentials") == 0
    event = await env.row("SELECT detail FROM auth_events WHERE kind = 'passkey_register_failed'")
    assert set(event.detail) == {"reason"}


async def test_the_ed25519_algorithm_is_accepted(env: Env) -> None:
    browser = await signed_in(env)
    assert (await register(browser, SoftAuthenticator(), algorithm="ed25519")).status_code == 201


async def test_a_challenge_is_single_use(env: Env) -> None:
    browser = await signed_in(env)
    auth = SoftAuthenticator()
    options = await register_options(browser)
    credential = auth.create(options)
    first = await browser.post(f"{BASE}/register", {"credential": credential})
    assert first.status_code == 201
    await env.run("DELETE FROM webauthn_credentials")
    replay = await browser.post(f"{BASE}/register", {"credential": credential})
    assert replay.status_code == 400


async def test_a_challenge_of_another_session_is_refused(env: Env) -> None:
    """The challenge is bound to the session that asked for it."""
    first = await signed_in(env)
    options = await register_options(first)
    second = Browser(env)
    assert (await second.sign_in("user@example.com")).status_code == 200
    await env.run("UPDATE mfa_totp SET last_step = 0")  # the same TOTP step again
    await pass_second_factor(env, second, await only_user_id(env))
    answer = await second.post(
        f"{BASE}/register", {"credential": SoftAuthenticator().create(options)}
    )
    assert answer.status_code == 400
    assert await env.scalar("SELECT count(*) FROM webauthn_credentials") == 0


async def test_an_expired_challenge_is_refused(env: Env) -> None:
    browser = await signed_in(env)
    options = await register_options(browser)
    await env.run("UPDATE webauthn_challenges SET expires_at = now() - interval '1 second'")
    answer = await browser.post(
        f"{BASE}/register", {"credential": SoftAuthenticator().create(options)}
    )
    assert answer.status_code == 400


async def _fill(env: Env, count: int) -> None:
    await env.run(
        "INSERT INTO webauthn_credentials (user_id, credential_id, public_key, name) "
        "SELECT id, sha256((id::text || g::text)::bytea), 'k', 'Passkey' "
        "FROM users, generate_series(1, :n) g",
        n=count,
    )


async def test_the_eleventh_passkey_is_refused(env: Env) -> None:
    browser = await signed_in(env)
    await _fill(env, 10)
    refused = await browser.post(f"{BASE}/register/options")
    assert (refused.status_code, reason(refused)) == (409, "passkey_limit")


async def test_the_limit_holds_when_a_passkey_arrives_after_the_options(env: Env) -> None:
    browser = await signed_in(env)
    await _fill(env, 9)
    options = await register_options(browser)
    await env.run(
        "INSERT INTO webauthn_credentials (user_id, credential_id, public_key, name) "
        "SELECT id, sha256('extra'), 'k', 'Passkey' FROM users"
    )
    late = await browser.post(
        f"{BASE}/register", {"credential": SoftAuthenticator().create(options)}
    )
    assert (late.status_code, reason(late)) == (409, "passkey_limit")
    assert await env.scalar("SELECT count(*) FROM webauthn_credentials") == 10


async def test_a_credential_id_already_registered_is_refused(env: Env) -> None:
    browser = await signed_in(env)
    auth = SoftAuthenticator()
    assert (await register(browser, auth)).status_code == 201
    again = await register(browser, auth, credential_id=auth.credentials[0].credential_id)
    assert again.status_code == 400
    assert await env.scalar("SELECT count(*) FROM webauthn_credentials") == 1


async def test_registration_needs_a_full_session(env: Env) -> None:
    browser = await signed_in(env, second_factor=False)
    answer = await browser.post(f"{BASE}/register/options")
    assert (answer.status_code, reason(answer)) == (403, "two_factor_required")
    anonymous = await Browser(env).post(f"{BASE}/register/options")
    assert anonymous.status_code == 401
