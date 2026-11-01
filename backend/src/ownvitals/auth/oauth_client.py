"""Talking to the providers with Authlib's ``AsyncOAuth2Client`` (httpx).

The authorization URL carries ``response_mode=query`` (a POST callback would not carry the Lax
flow cookie) and PKCE S256. A re-authentication also sends ``max_age=0`` and, for Microsoft,
``prompt=login``; for Google it also asks for ``auth_time`` explicitly with the OIDC ``claims``
parameter (ADR 0003 section 5). The code exchange and the GitHub API calls go through
``OAuthRuntime.transport``, which tests replace with an in-process fake; no code path here logs a
code, token, secret or query string.
"""

import json
import re
import warnings
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

with warnings.catch_warnings():
    # Authlib's own import of its deprecated jose module; we use joserfc directly.
    warnings.simplefilter("ignore")
    from authlib.integrations.httpx_client import AsyncOAuth2Client

from ownvitals.auth.oauth_idtoken import (
    IdentityClaims,
    IdTokenError,
    Jwks,
    JwksCache,
    verify_id_token,
)
from ownvitals.auth.oauth_providers import GITHUB, GOOGLE, MICROSOFT, Provider
from ownvitals.egress import OutboundBlocked, refuse_in_worker

TIMEOUT = httpx.Timeout(10.0)
GITHUB_API = "https://api.github.com"
# OIDC Core 5.5: ask for the auth_time claim in the ID token (Google does not add it by default).
AUTH_TIME_CLAIMS = json.dumps(
    {"id_token": {"auth_time": {"essential": True}}}, separators=(",", ":")
)


class ProviderError(Exception):
    """The provider refused or answered nonsense. Never carries a token or response body."""


_MAX_AGE = re.compile(r"max-age=(\d+)", re.IGNORECASE)


def _max_age(cache_control: str | None) -> float | None:
    """Seconds the provider allows caching: ``max-age=N``; 0 for ``no-cache`` and ``no-store``
    (``JwksCache`` clamps it to its minimum); None when absent or malformed (the default)."""
    seconds: float | None = None
    for directive in (cache_control or "").split(","):
        directive = directive.strip().lower()
        if directive in ("no-cache", "no-store") or directive.startswith("no-cache="):
            return 0.0
        if match := _MAX_AGE.fullmatch(directive):
            seconds = float(match.group(1))
    return seconds


async def _fetch_jwks_with(transport: httpx.AsyncBaseTransport | None, url: str) -> Jwks:
    refuse_in_worker(urlsplit(url).hostname)  # web only (ADR 0006 section 9)
    async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT) as client:
        response = await client.get(url)
        response.raise_for_status()
        return Jwks(response.json(), _max_age(response.headers.get("cache-control")))


@dataclass(slots=True)
class OAuthRuntime:
    """The enabled providers, the JWKS cache and the (replaceable) HTTP transport."""

    providers: dict[str, Provider]
    transport: httpx.AsyncBaseTransport | None = None
    jwks: JwksCache = field(init=False)

    def __post_init__(self) -> None:
        self.jwks = JwksCache(self._fetch)

    async def _fetch(self, url: str) -> Jwks:
        return await _fetch_jwks_with(self.transport, url)


def _client(provider: Provider, redirect_uri: str, transport: Any) -> Any:
    refuse_in_worker(urlsplit(provider.token_url).hostname)  # web only (ADR 0006 section 9)
    return AsyncOAuth2Client(
        client_id=provider.client_id,
        client_secret=provider.client_secret,
        scope=provider.scope,
        redirect_uri=redirect_uri,
        token_endpoint_auth_method="client_secret_post",  # noqa: S106
        code_challenge_method="S256",
        timeout=TIMEOUT,
        transport=transport,
    )


def authorization_url(
    provider: Provider,
    redirect_uri: str,
    *,
    state: str,
    nonce: str | None,
    verifier: str,
    reauth: bool,
) -> str:
    extra: dict[str, str] = {"response_mode": "query"} if provider.oidc else {}
    if provider.oidc and nonce:
        extra["nonce"] = nonce
    if reauth and provider.oidc:
        extra["max_age"] = "0"
        if provider.name == MICROSOFT:
            extra["prompt"] = "login"
        elif provider.name == GOOGLE:
            extra["claims"] = AUTH_TIME_CLAIMS
    url, _ = _client(provider, redirect_uri, None).create_authorization_url(
        provider.authorize_url, state=state, code_verifier=verifier, **extra
    )
    return url


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    subject: str
    email: str | None
    email_verified: bool
    auth_time: int | None  # None for GitHub, which has no such claim


async def identify(
    runtime: OAuthRuntime,
    provider: Provider,
    redirect_uri: str,
    *,
    code: str,
    verifier: str,
    nonce: str | None,
) -> ProviderIdentity:
    """Exchange the code and return who signed in. Raises ``ProviderError`` for any failure."""
    client = _client(provider, redirect_uri, runtime.transport)
    try:
        async with client:
            token = await client.fetch_token(
                provider.token_url,
                code=code,
                code_verifier=verifier,
                grant_type="authorization_code",
                headers={"Accept": "application/json"},
            )
            if provider.oidc:
                id_token = token.get("id_token")
                if not isinstance(id_token, str) or nonce is None:
                    raise ProviderError("no ID token")
                claims: IdentityClaims = await verify_id_token(
                    provider, id_token, nonce=nonce, cache=runtime.jwks
                )
                return ProviderIdentity(
                    claims.subject, claims.email, claims.email_verified, claims.auth_time
                )
            return await _github_identity(client)
    except (IdTokenError, ProviderError) as exc:
        raise ProviderError(str(exc)) from None
    except OutboundBlocked:
        raise  # the worker's refusal keeps its own category (``outbound_blocked``), not "provider"
    except Exception as exc:  # httpx, Authlib: the text can hold URLs or bodies; keep the class
        raise ProviderError(f"provider call failed ({type(exc).__name__})") from None


async def _github_identity(client: Any) -> ProviderIdentity:
    headers = {"Accept": "application/vnd.github+json"}
    user = await client.get(f"{GITHUB_API}/user", headers=headers)
    user.raise_for_status()
    ident = user.json().get("id")
    if isinstance(ident, bool) or not isinstance(ident, int):
        raise ProviderError("no numeric GitHub id")  # never the login name
    emails = await client.get(f"{GITHUB_API}/user/emails", headers=headers)
    emails.raise_for_status()
    email, verified = None, False
    for entry in emails.json():
        if isinstance(entry, dict) and entry.get("primary") is True:
            address = entry.get("email")
            if isinstance(address, str) and address:
                email, verified = address, entry.get("verified") is True
            break
    return ProviderIdentity(str(ident), email, verified, None)


__all__ = [
    "GITHUB",
    "OAuthRuntime",
    "ProviderError",
    "ProviderIdentity",
    "authorization_url",
    "identify",
]
