"""PKCE for Google sign-in (#10): every flow gets its own verifier, stored server-side with the
state; the authorization URL carries its S256 challenge; the code exchange sends that stored
verifier; and a flow without a well-formed verifier never reaches Google."""

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy.exc import IntegrityError

from authtools import Env, auth_schema_fixture, clean_auth_fixture, smtp_fixture  # noqa: F401 - fixtures
from flows import Browser
from oauthtools import fake_fixture, location, make_invite, oenv_fixture, start  # noqa: F401 - fixtures
from oidcfake import FakeProviders
from ownvitals.auth.oauth_client import ProviderError, authorization_url, identify

CALLBACK = "https://ownvitals.example/oauth/google/callback"


def challenge_of(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def started(env: Env) -> tuple[Browser, str, str]:
    browser = Browser(env)
    response = await start(browser, "google", invite_code=await make_invite(env))
    assert response.status_code == 200, response.text
    verifier = await env.scalar("SELECT pkce_verifier FROM oauth_flows ORDER BY created_at DESC")
    return browser, response.json()["authorization_url"], str(verifier)


async def test_each_flow_stores_its_own_verifier_and_the_url_carries_its_challenge(
    oenv: Env,
) -> None:
    _, url, verifier = await started(oenv)
    assert len(verifier) == 64
    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [challenge_of(verifier)]
    assert verifier not in url  # only the challenge leaves the server
    await started(oenv)
    assert await oenv.scalar("SELECT count(DISTINCT pkce_verifier) FROM oauth_flows") == 2


async def test_the_code_exchange_sends_the_stored_verifier(oenv: Env, fake: FakeProviders) -> None:
    browser, url, _ = await started(oenv)
    callback = fake.complete(url)
    # Another well-formed verifier in the row: the fake's S256 check fails, so the exchange
    # sends what the row holds, not something derived from the URL or the request.
    await oenv.run("UPDATE oauth_flows SET pkce_verifier = repeat('w', 64)")
    assert location(await browser.get(callback)) == "/signin?oauth=invalid"
    assert fake.token_requests == 1
    assert await oenv.scalar("SELECT count(*) FROM users") == 0


async def test_with_the_stored_verifier_the_same_flow_signs_up(
    oenv: Env, fake: FakeProviders
) -> None:
    browser, url, _ = await started(oenv)
    assert location(await browser.get(fake.complete(url))) == "/"
    assert fake.token_requests == 1


@pytest.mark.parametrize(
    "value",
    ["NULL", "''", "repeat('v', 43)", "repeat('v', 65)", "repeat('v', 63) || '+'"],
)
async def test_a_flow_without_a_full_verifier_cannot_be_stored(oenv: Env, value: str) -> None:
    await started(oenv)
    with pytest.raises(IntegrityError):
        await oenv.run(f"UPDATE oauth_flows SET pkce_verifier = {value}")  # noqa: S608 - the parameters above


@pytest.mark.parametrize("verifier", ["", "short", "x" * 129, "has space" * 8, None])
async def test_a_callback_without_a_verifier_sends_nothing(
    oenv: Env, fake: FakeProviders, verifier: object
) -> None:
    runtime = oenv.app.state.oauth
    provider = runtime.providers["google"]
    with pytest.raises(ProviderError, match="PKCE"):
        await identify(
            runtime,
            provider,
            CALLBACK,
            code="c",
            verifier=verifier,  # pyright: ignore[reportArgumentType]
            nonce="n",
        )
    assert fake.token_requests == 0
    with pytest.raises(ProviderError, match="PKCE"):
        await authorization_url(
            provider,
            CALLBACK,
            state="s",
            nonce="n",
            verifier=verifier,  # pyright: ignore[reportArgumentType]
            reauth=False,
        )
