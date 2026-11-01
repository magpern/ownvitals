"""ID-token validation against the fake provider (ADR 0003 section 5): no database, no network."""

import asyncio
import time
from typing import Any

import pytest

from ownvitals.auth.oauth_idtoken import IdTokenError, Jwks, JwksCache, verify_id_token
from ownvitals.auth.oauth_providers import Provider, enabled_providers
from ownvitals.config import Settings
from oidcfake import DROP, GOOGLE_JWKS, MICROSOFT_JWKS, TENANT, FakeProviders

NONCE = "the-nonce"
URL = "https://x/auth?client_id={cid}&nonce=" + NONCE


@pytest.fixture
def providers(fake: FakeProviders) -> dict[str, Provider]:
    values: dict[str, Any] = {}
    for name, cid in fake.client_ids.items():
        values[f"oauth_{name}_client_id"] = cid
        values[f"oauth_{name}_client_secret"] = "s"
    return enabled_providers(Settings(**values))


async def fetch(fake: FakeProviders, max_age: float | None = None):
    async def inner(url: str) -> Jwks:
        fake.jwks_fetches += 1
        return Jwks({"keys": [k.as_dict(private=False) for k in fake.published]}, max_age)

    return inner


async def verify(
    fake: FakeProviders, provider: Provider, token: str, cache: JwksCache | None = None
):
    cache = cache or JwksCache(await fetch(fake))
    return await verify_id_token(provider, token, nonce=NONCE, cache=cache)


def token(fake: FakeProviders, provider: Provider, **kwargs: Any) -> str:
    sign_args = {k: kwargs.pop(k) for k in ("alg", "key", "kid") if k in kwargs}
    url = URL.format(cid=provider.client_id)
    kwargs.setdefault("nonce", NONCE)
    return fake.sign(fake.claims_for(provider.name, url, **kwargs), **sign_args)


async def test_a_good_google_token_is_accepted(fake: FakeProviders, providers) -> None:
    claims = await verify(fake, providers["google"], token(fake, providers["google"]))
    assert (claims.subject, claims.email, claims.email_verified) == ("sub-1", "g@example.com", True)


async def test_microsoft_subject_is_tid_slash_oid_and_the_email_is_never_verified(
    fake: FakeProviders, providers
) -> None:
    claims = await verify(fake, providers["microsoft"], token(fake, providers["microsoft"]))
    assert claims.subject == f"{TENANT}/oid-1"
    assert claims.email_verified is False


@pytest.mark.parametrize(
    ("name", "override"),
    [
        ("aud", {"aud": "someone-else"}),
        ("iss", {"iss": "https://evil.example"}),
        ("nonce", {"nonce": "other"}),
        ("nonce missing", {"nonce": DROP}),
        ("expired", {"exp": int(time.time()) - 3600}),
        ("iat in the future", {"iat": int(time.time()) + 3600}),
        ("exp missing", {"exp": DROP}),
        ("sub missing", {"sub": DROP}),
    ],
)
async def test_bad_claims_are_refused(fake: FakeProviders, providers, name: str, override) -> None:
    with pytest.raises(IdTokenError):
        await verify(fake, providers["google"], token(fake, providers["google"], **override))


async def test_microsoft_issuer_must_match_the_tokens_own_tenant(fake, providers) -> None:
    other = {"iss": "https://login.microsoftonline.com/another-tenant/v2.0"}
    with pytest.raises(IdTokenError, match="tenant"):
        await verify(fake, providers["microsoft"], token(fake, providers["microsoft"], **other))
    with pytest.raises(IdTokenError, match="tid"):
        await verify(fake, providers["microsoft"], token(fake, providers["microsoft"], oid=DROP))


@pytest.mark.parametrize("alg", ["none", "HS256"])
async def test_only_rs256_is_accepted(fake: FakeProviders, providers, alg: str) -> None:
    with pytest.raises(IdTokenError, match="RS256"):
        await verify(fake, providers["google"], token(fake, providers["google"], alg=alg))


async def test_a_token_signed_with_another_key_is_refused(fake: FakeProviders, providers) -> None:
    with pytest.raises(IdTokenError, match="signature"):
        await verify(fake, providers["google"], token(fake, providers["google"], key=fake.other))


async def test_leeway_is_60_seconds(fake: FakeProviders, providers) -> None:
    now = int(time.time())
    inside = token(fake, providers["google"], exp=now - 30, iat=now - 600)
    assert (await verify(fake, providers["google"], inside)).subject == "sub-1"
    outside = token(fake, providers["google"], exp=now - 120, iat=now - 600)
    with pytest.raises(IdTokenError, match="expired"):
        await verify(fake, providers["google"], outside)
    skewed = token(fake, providers["google"], iat=now + 30)
    assert (await verify(fake, providers["google"], skewed)).subject == "sub-1"


@pytest.mark.parametrize(
    ("override", "verified"),
    [
        ({"email_verified": False}, False),
        ({"email_verified": DROP}, False),
        ({"email": DROP}, False),
        ({"email_verified": "true"}, True),
    ],
)
async def test_google_email_counts_as_verified_only_when_stated(
    fake: FakeProviders, providers, override, verified: bool
) -> None:
    claims = await verify(fake, providers["google"], token(fake, providers["google"], **override))
    assert claims.email_verified is verified


async def test_jwks_is_refetched_on_an_unknown_kid_at_most_once_a_minute(
    fake: FakeProviders, providers
) -> None:
    now = [1000.0]
    cache = JwksCache(await fetch(fake), clock=lambda: now[0])
    google = providers["google"]
    await verify(fake, google, token(fake, google), cache)
    await verify(fake, google, token(fake, google), cache)
    assert fake.jwks_fetches == 1  # a known kid is served from the cache
    rotated = token(fake, google, key=fake.rotated)  # the provider rotated, not yet published
    with pytest.raises(IdTokenError, match="rate limit"):
        await verify(fake, google, rotated, cache)  # within a minute of the last fetch
    assert fake.jwks_fetches == 1
    now[0] += 61
    for _ in range(3):
        with pytest.raises(IdTokenError, match="unknown key id"):
            await verify(fake, google, rotated, cache)
    assert fake.jwks_fetches == 2  # one refetch, then rate limited
    fake.published.append(fake.rotated)
    with pytest.raises(IdTokenError, match="rate limit"):
        await verify(fake, google, rotated, cache)
    assert fake.jwks_fetches == 2
    now[0] += 61
    assert (await verify(fake, google, rotated, cache)).subject == "sub-1"
    assert fake.jwks_fetches == 3


def test_the_jwks_urls_are_the_providers_own() -> None:
    assert GOOGLE_JWKS.startswith("https://www.googleapis.com/")
    assert MICROSOFT_JWKS.startswith("https://login.microsoftonline.com/")


# --- review of #53: concurrency, key lifetime, azp --------------------------------------------


async def test_concurrent_sign_ins_with_a_new_kid_share_one_fetch(
    fake: FakeProviders, providers
) -> None:
    fake.published.append(fake.rotated)
    google = providers["google"]
    fetches = 0

    async def slow(url: str) -> Jwks:
        nonlocal fetches
        fetches += 1
        await asyncio.sleep(0.05)
        return Jwks({"keys": [k.as_dict(private=False) for k in fake.published]})

    cache = JwksCache(slow)
    rotated = token(fake, google, key=fake.rotated)
    results = await asyncio.gather(*(verify(fake, google, rotated, cache) for _ in range(5)))
    assert [r.subject for r in results] == ["sub-1"] * 5  # none refused by the 60 s limit
    assert fetches == 1


async def test_a_failed_fetch_is_not_retried_within_the_minute(
    fake: FakeProviders, providers
) -> None:
    calls = 0

    async def broken(url: str) -> Jwks:
        nonlocal calls
        calls += 1
        raise OSError("down")

    cache = JwksCache(broken, clock=lambda: 1000.0)
    google = providers["google"]
    for expected in ("could not be fetched", "rate limit", "rate limit"):
        with pytest.raises(IdTokenError, match=expected):
            await verify(fake, google, token(fake, google), cache)
    assert calls == 1


@pytest.mark.parametrize(
    ("max_age", "lifetime"),
    [
        (None, 3600),  # the default
        (600, 600),  # honoured
        (5, 60),  # at least the refetch interval
        (10 * 24 * 3600, 24 * 3600),  # capped at 24 h
        (0, 60),  # no-cache, no-store, max-age=0: the minimum, not the default
    ],
)
async def test_cached_keys_expire_after_the_providers_max_age_within_limits(
    fake: FakeProviders, providers, max_age: float | None, lifetime: int
) -> None:
    now = [1000.0]
    cache = JwksCache(await fetch(fake, max_age), clock=lambda: now[0])
    google = providers["google"]
    await verify(fake, google, token(fake, google), cache)
    now[0] += lifetime - 1
    await verify(fake, google, token(fake, google), cache)
    assert fake.jwks_fetches == 1  # still served from the cache
    now[0] += 2
    await verify(fake, google, token(fake, google), cache)
    assert fake.jwks_fetches == 2  # expired: fetched again


async def test_a_key_the_provider_retired_stops_being_accepted(
    fake: FakeProviders, providers
) -> None:
    now = [1000.0]
    cache = JwksCache(await fetch(fake, 600), clock=lambda: now[0])
    google = providers["google"]
    assert (await verify(fake, google, token(fake, google), cache)).subject == "sub-1"
    fake.published.clear()
    fake.published.append(fake.rotated)  # the provider retired "good-key"
    now[0] += 601
    with pytest.raises(IdTokenError, match="unknown key id"):
        await verify(fake, google, token(fake, google), cache)


async def test_a_failed_refetch_keeps_the_keys_that_have_not_expired(
    fake: FakeProviders, providers
) -> None:
    """A forged kid during a provider outage must not turn sign-in off."""
    now = [1000.0]
    healthy = await fetch(fake, 600)
    down = False

    async def flaky(url: str) -> Jwks:
        if down:
            raise OSError("down")
        return await healthy(url)

    cache = JwksCache(flaky, clock=lambda: now[0])
    google = providers["google"]
    await verify(fake, google, token(fake, google), cache)
    down = True
    now[0] += 120  # not expired, and past the refetch limit
    forged = token(fake, google, kid="forged-kid")
    with pytest.raises(IdTokenError, match="could not be fetched"):
        await verify(fake, google, forged, cache)
    assert (await verify(fake, google, token(fake, google), cache)).subject == "sub-1"
    now[0] += 600  # now they have expired: no stale key is served
    with pytest.raises(IdTokenError):
        await verify(fake, google, token(fake, google), cache)


async def test_a_failed_refresh_drops_the_expired_keys(fake: FakeProviders, providers) -> None:
    now = [1000.0]
    healthy = await fetch(fake, 600)
    down = False

    async def flaky(url: str) -> Jwks:
        if down:
            raise OSError("down")
        return await healthy(url)

    cache = JwksCache(flaky, clock=lambda: now[0])
    google = providers["google"]
    await verify(fake, google, token(fake, google), cache)
    down = True
    now[0] += 601
    with pytest.raises(IdTokenError, match="could not be fetched"):
        await verify(fake, google, token(fake, google), cache)
    now[0] += 30  # no stale key is used while the refetch limit holds
    with pytest.raises(IdTokenError):
        await verify(fake, google, token(fake, google), cache)


async def test_several_audiences_need_azp_to_be_our_client_id(
    fake: FakeProviders, providers
) -> None:
    google = providers["google"]
    ours = google.client_id
    multi = {"aud": [ours, "https://other.example"]}
    for override in (multi, {**multi, "azp": "https://other.example"}):
        with pytest.raises(IdTokenError, match="authorized party"):
            await verify(fake, google, token(fake, google, **override))
    accepted = await verify(fake, google, token(fake, google, **multi, azp=ours))
    assert accepted.subject == "sub-1"
    # A single audience needs no azp, and a list of one neither.
    assert (await verify(fake, google, token(fake, google))).subject == "sub-1"
    assert (await verify(fake, google, token(fake, google, aud=[ours]))).subject == "sub-1"


# --- Cache-Control parsing and the HTTP fetch (oauth_client) ------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("max-age=300", 300.0),
        ("public, max-age=19000, must-revalidate", 19000.0),
        ("MAX-AGE=45", 45.0),
        ("private,max-age=30", 30.0),
        ("max-age=0", 0.0),
        ("no-cache", 0.0),
        ("no-store", 0.0),
        ("no-store, max-age=600", 0.0),
        ('no-cache="set-cookie"', 0.0),
        ("max-age=99999999999", 99999999999.0),  # huge: the cache caps it
        (None, None),
        ("", None),
        ("public", None),
        ("max-age=abc", None),
        ("max-age=-5", None),
        ('max-age="300"', None),
        ("max-age = 300", None),
        ("s-maxage=10", None),
        ("max-age=", None),
        ("max-age=1.5", None),
    ],
)
def test_max_age_is_read_from_cache_control(header: str | None, expected: float | None) -> None:
    from ownvitals.auth.oauth_client import _max_age

    assert _max_age(header) == expected


@pytest.mark.parametrize(
    ("header", "lifetime"),
    [
        ("max-age=600", 600),
        ("max-age=5", 60),
        ("max-age=0", 60),
        ("no-store", 60),
        ("max-age=99999999999", 24 * 3600),
        ("max-age=abc", 3600),
        (None, 3600),
    ],
)
async def test_the_fetch_sends_the_header_through_to_the_cache_lifetime(
    fake: FakeProviders, providers, header: str | None, lifetime: int
) -> None:
    """End to end: a real HTTP response (the fake JWKS endpoint) -> _fetch_jwks_with -> cache."""
    from ownvitals.auth.oauth_client import _fetch_jwks_with
    from oidcfake import GOOGLE_JWKS

    fake.jwks_cache_control = header
    now = [1000.0]

    async def fetcher(url: str) -> Jwks:
        return await _fetch_jwks_with(fake.transport, url)

    cache = JwksCache(fetcher, clock=lambda: now[0])
    google = providers["google"]
    assert google.jwks_url == GOOGLE_JWKS
    await verify(fake, google, token(fake, google), cache)
    now[0] += lifetime - 1
    await verify(fake, google, token(fake, google), cache)
    assert fake.jwks_fetches == 1
    now[0] += 2
    await verify(fake, google, token(fake, google), cache)
    assert fake.jwks_fetches == 2


async def test_the_fetch_returns_the_document_and_refuses_an_error_status(
    fake: FakeProviders,
) -> None:
    from ownvitals.auth.oauth_client import _fetch_jwks_with
    from oidcfake import GOOGLE_JWKS

    fake.jwks_cache_control = "max-age=77"
    jwks = await _fetch_jwks_with(fake.transport, GOOGLE_JWKS)
    assert jwks.max_age == 77.0
    assert [k["kid"] for k in jwks.document["keys"]] == ["good-key"]
    fake.jwks_status = 503
    import httpx

    with pytest.raises(httpx.HTTPStatusError):
        await _fetch_jwks_with(fake.transport, GOOGLE_JWKS)
