"""The language preference (ADR 0015 sections 1 and 2; #249): the manifest, the request locale,
``PUT /api/account/locale``, the session's ``locale`` and the language a sign-up stores.

Synthetic accounts only; Google is the in-process fake (``oidcfake``).
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    menv_fixture,
    running_env,
    smtp_fixture,
)
from flows import PASSWORD, Browser, pass_second_factor, reason, signed_in, user_with_password
from oauthtools import fake_fixture, location, make_invite, oenv_fixture, start  # noqa: F401 - fixtures
from oidcfake import FakeProviders
from ownvitals import locale_policy, locales
from ownvitals.auth import tables
from ownvitals.config import Settings
from smtpfake import SmtpFake
from test_auth_signup import EMAIL, confirm, invite, sign_up

LOCALE = "/api/account/locale"
REPO = Path(__file__).resolve().parents[2]
OFFERED = ("en", "sv")


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]


# --- the manifest and the pure policy (no database) -------------------------------------------


def test_the_backend_reads_the_repository_manifest() -> None:
    manifest = json.loads((REPO / "frontend" / "src" / "i18n" / "locales.json").read_text())
    assert tuple(manifest) == locales.SUPPORTED_LOCALES


def test_every_locale_check_allows_exactly_the_manifest() -> None:
    allowed = tuple(re.findall(r"'([a-z]{2})'", tables.LOCALE_CHECK))
    assert allowed == locales.SUPPORTED_LOCALES


@pytest.mark.parametrize(
    "raw", ['"en"', '["en", "SV"]', '["en", "swe"]', '["sv"]', "[1]", '{"en": 1}']
)
def test_a_malformed_manifest_is_refused(raw: str) -> None:
    with pytest.raises(RuntimeError):
        locales.parse_manifest(raw)


def test_the_operator_can_switch_a_language_off_but_never_add_one() -> None:
    def offered(value: str) -> tuple[str, ...]:
        enabled = settings(locales=value).enabled_locales
        return locale_policy.offered_locales(locales.SUPPORTED_LOCALES, enabled)

    assert offered("en,sv") == OFFERED
    assert offered(" SV ") == OFFERED  # en is always on
    assert offered("en") == ("en",)
    assert offered("fr,de,xx9,") == ("en",)  # not in the manifest: never offered
    assert settings().locales == "en,sv"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("sv", "sv"),
        ("sv-SE,sv;q=0.9,en;q=0.8", "sv"),
        ("en-GB, sv;q=0.5", "en"),
        ("de, sv;q=0.2, en;q=0.1", "sv"),
        ("sv;q=0", None),
        ("sv;q=banana", None),
        ("sv;q=nan", None),
        ("sv;q=2", None),
        ("fr, de", None),
        ("*", None),
        ("", None),
        (None, None),
        (";;;,,,=;q=", None),
        ("\x00\xffé;q=\x7f", None),
        # Only the first 8 entries count ...
        (",".join(["fr"] * 8 + ["sv"]), None),
        (",".join(["fr"] * 7 + ["sv"]), "sv"),
        # ... and only the first 256 bytes.
        ("x" * 256 + ",sv", None),
        ("fr," + "x" * 8192 + ",sv", None),
    ],
)
def test_accept_language_is_bounded_and_never_raises(header: str | None, expected: str | None):
    assert locale_policy.parse_accept_language(header, OFFERED) == expected


def test_the_request_locale_is_the_field_then_the_header_then_english() -> None:
    def request(field: object, header: str | None) -> str:
        return locale_policy.request_locale(OFFERED, field, header)

    assert request("sv", "en") == "sv"
    assert request("fr", "sv") == "sv"  # an unsupported field is dropped, not echoed
    assert request("sv" * 20, "sv") == "sv"
    assert request(["sv"], None) == "en"
    assert request(None, "x" * 300) == "en"  # an oversized header falls back to en
    assert request(None, None) == "en"


def test_a_new_account_stores_nothing_while_one_language_is_offered() -> None:
    assert locale_policy.storable_locale(OFFERED, "sv") == "sv"
    assert locale_policy.storable_locale(OFFERED, "fr") is None
    assert locale_policy.storable_locale(("en",), "en") is None


# --- the columns ------------------------------------------------------------------------------


async def test_the_migrated_checks_allow_exactly_the_manifest(env: Env) -> None:
    for table in ("users", "pending_signups", "oauth_flows"):
        definition = await env.scalar(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :c",
            c=f"{table}_locale",
        )
        assert tuple(re.findall(r"'([a-z]{2})'", str(definition))) == locales.SUPPORTED_LOCALES


async def test_the_check_refuses_a_code_outside_the_allowlist(env: Env) -> None:
    user_id = await env.user()
    statements = [
        "UPDATE users SET locale = :v WHERE id = :u",
        "INSERT INTO pending_signups (email, email_normalized, expires_at, locale) "
        "VALUES ('p@example.com', 'p@example.com', now(), :v)",
        "INSERT INTO oauth_flows (flow_hash, provider, purpose, state_hash, nonce, "
        "pkce_verifier, expires_at, locale) VALUES (:h, 'google', 'sign_in', '\\x02', 'n', "
        "repeat('a', 64), now(), :v)",
    ]
    for statement in statements:
        for value in ("fr", "SV", "sv-SE", ""):
            with pytest.raises(IntegrityError, match="_locale"):
                async with env.db() as db:
                    await db.execute(text(statement), {"v": value, "u": user_id, "h": b"x"})
        for value in (None, *locales.SUPPORTED_LOCALES):  # NULL and every manifest code pass
            async with env.db() as db:
                params = {"v": value, "u": user_id, "h": str(value).encode()}
                await db.execute(text(statement), params)


async def test_accounts_the_cli_or_an_admin_create_follow_the_browser(env: Env) -> None:
    user_id = await env.user("cli@example.com")  # ``accounts.create_user``, as the CLI paths
    assert await env.scalar("SELECT locale FROM users WHERE id = :u", u=user_id) is None


# --- PUT /api/account/locale and the session ---------------------------------------------------


async def session_locale(browser: Browser) -> object:
    return (await browser.get("/api/auth/session")).json()["user"]["locale"]


async def test_the_user_sets_and_clears_their_own_language(menv: Env) -> None:
    browser = await signed_in(menv)
    other = await menv.user("other@example.com")
    assert await session_locale(browser) is None
    response = await browser.request("PUT", LOCALE, {"locale": "sv"})
    assert (response.status_code, response.json()) == (200, {"locale": "sv"})
    assert await session_locale(browser) == "sv"
    assert await menv.scalar("SELECT locale FROM users WHERE id = :u", u=other) is None
    response = await browser.request("PUT", LOCALE, {"locale": None})
    assert (response.status_code, response.json()) == (200, {"locale": None})
    assert await session_locale(browser) is None


@pytest.mark.parametrize("value", ["fr", "SV", "sv-SE", "", "x" * 2000, 1, ["sv"], {"a": "sv"}])
async def test_a_value_that_is_not_offered_is_refused_and_not_echoed(
    menv: Env, value: object
) -> None:
    browser = await signed_in(menv)
    response = await browser.request("PUT", LOCALE, {"locale": value})
    assert (response.status_code, reason(response)) == (400, "locale_not_offered")
    assert "x" * 20 not in response.text
    assert "fr" not in response.text
    assert await menv.scalar("SELECT locale FROM users") is None


async def test_the_body_must_name_the_locale(menv: Env) -> None:
    browser = await signed_in(menv)
    assert (await browser.request("PUT", LOCALE, {})).status_code == 422


async def test_the_write_needs_a_full_session_and_the_csrf_token(menv: Env) -> None:
    assert (await Browser(menv).request("PUT", LOCALE, {"locale": "sv"})).status_code == 401
    half = Browser(menv)
    user_id = await user_with_password(menv, "half@example.com")
    assert (await half.sign_in("half@example.com")).status_code == 200
    response = await half.request("PUT", LOCALE, {"locale": "sv"})
    assert (response.status_code, reason(response)) == (403, "two_factor_required")
    await pass_second_factor(menv, half, user_id)
    forged = await half.request("PUT", LOCALE, {"locale": "sv"}, csrf="forged")
    assert forged.status_code == 403
    assert await menv.scalar("SELECT locale FROM users") is None


async def test_a_language_the_operator_switched_off_is_neither_set_nor_shown(
    app_database_url: str, clean_auth: None
) -> None:
    async with running_env(app_database_url, locales="en") as env:
        browser = await signed_in(env)
        await env.scalar("UPDATE users SET locale = 'sv' RETURNING id")
        assert await session_locale(browser) is None  # stored, but no longer offered
        response = await browser.request("PUT", LOCALE, {"locale": "sv"})
        assert (response.status_code, reason(response)) == (400, "locale_not_offered")
        assert (await browser.request("PUT", LOCALE, {"locale": "en"})).status_code == 200


# --- sign-up stores the language in effect ----------------------------------------------------


@pytest.mark.parametrize(
    ("field", "header", "stored"),
    [("sv", None, "sv"), (None, "sv-SE,sv;q=0.9", "sv"), ("fr", "de", "en"), (None, None, "en")],
)
async def test_an_email_sign_up_stores_its_language_through_the_pending_row(
    menv: Env, smtp: SmtpFake, field: str | None, header: str | None, stored: str
) -> None:
    if header is not None:
        menv.client.headers["accept-language"] = header
    browser = Browser(menv)
    body = {"email": EMAIL, "password": PASSWORD, "locale": field}
    body["invite_code"] = await invite(menv)
    assert (await browser.post("/api/auth/signup", body)).status_code == 202
    assert await menv.scalar("SELECT locale FROM pending_signups") == stored
    menv.client.headers.pop("accept-language", None)
    response = await confirm(Browser(menv), smtp.to(EMAIL)[-1].token(), body["password"])
    assert response.status_code == 200
    assert await menv.scalar("SELECT locale FROM users") == stored


async def test_an_email_sign_up_without_a_locale_field_still_works(
    menv: Env, smtp: SmtpFake
) -> None:
    await sign_up(Browser(menv), await invite(menv))  # an older page: no ``locale`` key
    assert await menv.scalar("SELECT locale FROM pending_signups") == "en"


async def test_a_google_sign_up_stores_its_language_through_the_flow_row(
    oenv: Env, fake: FakeProviders
) -> None:
    browser = Browser(oenv)
    started = await start(browser, "google", invite_code=await make_invite(oenv), locale="sv")
    assert started.status_code == 200
    assert await oenv.scalar("SELECT locale FROM oauth_flows") == "sv"
    callback = fake.complete(started.json()["authorization_url"])
    assert location(await browser.get(callback)) == "/"
    assert await oenv.scalar("SELECT locale FROM users") == "sv"
    assert await session_locale(browser) == "sv"


async def test_a_google_hand_off_carries_the_flow_language_to_the_account(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    oenv.client.headers["accept-language"] = "sv"
    browser = Browser(oenv)
    started = await start(browser, "google", invite_code=await make_invite(oenv))
    callback = fake.complete(started.json()["authorization_url"], email_verified=False)
    assert location(await browser.get(callback)) == "/signin?oauth=email_needed"
    assert await oenv.scalar("SELECT locale FROM pending_signups") == "sv"
    oenv.client.headers["accept-language"] = "en"  # the confirming request's header is ignored
    await browser.post("/api/auth/oauth/pending/email", {"email": "new@example.com"})
    await asyncio.sleep(0.3)
    token = smtp.to("new@example.com")[0].token()
    assert (await browser.post("/api/auth/signup/confirm", {"token": token})).status_code == 200
    assert await oenv.scalar("SELECT locale FROM users") == "sv"


async def test_a_link_or_reauth_flow_stores_no_language(oenv: Env) -> None:
    browser = await signed_in(oenv, "linker@example.com")
    assert (await start(browser, "google", "link", locale="sv")).status_code == 200
    assert await oenv.scalar("SELECT purpose FROM oauth_flows") == "link"
    assert await oenv.scalar("SELECT locale FROM oauth_flows") is None
