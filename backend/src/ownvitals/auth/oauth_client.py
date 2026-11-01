"""Talking to Google with Authlib's ``AsyncOAuth2Client``, built by ``ownvitals.egress``.

``egress.google_oauth_client`` builds the client (PKCE S256, ``client_secret_post``) over Google's
sign-in transport (the token endpoint and JWKS hosts only, a total deadline, no redirects,
``trust_env=False``); ``egress.google_async_client`` fetches the JWKS. Nothing here imports a
network library: the guard refuses ``authlib.integrations`` and ``httpx`` outside the package.

The authorization URL carries ``response_mode=query`` (a POST callback would not carry the Lax
flow cookie), the nonce and the PKCE challenge of the flow's own ``code_verifier``. A
re-authentication also sends ``max_age=0`` and asks for ``auth_time`` explicitly with the OIDC
``claims`` parameter (ADR 0003 section 1, "Google OIDC"). The code exchange sends the same
``code_verifier``, read back from the flow row, so PKCE binds the code to this flow (#55's N15).
``OAuthRuntime.transport`` replaces the network transport in tests (the clients' ``inner``; every
egress check still runs); no code path here logs a code, token, secret or query string.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ownvitals import egress
from ownvitals.auth.oauth_idtoken import (
    IdentityClaims,
    IdTokenError,
    Jwks,
    JwksCache,
    verify_id_token,
)
from ownvitals.auth.oauth_providers import Provider
from ownvitals.egress import OutboundBlocked

if TYPE_CHECKING:  # annotations only
    import httpx

# OIDC Core 5.5: ask for the auth_time claim in the ID token (Google does not add it by default).
AUTH_TIME_CLAIMS = json.dumps(
    {"id_token": {"auth_time": {"essential": True}}}, separators=(",", ":")
)
# RFC 7636 section 4.1: 43 to 128 characters of the unreserved set.
VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")


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
    async with egress.google_async_client(inner=transport) as client:
        response = await client.get(url)
        response.raise_for_status()
        return Jwks(response.json(), _max_age(response.headers.get("cache-control")))


@dataclass(slots=True)
class OAuthRuntime:
    """The enabled providers, the JWKS cache and the (replaceable) network transport."""

    providers: dict[str, Provider]
    transport: httpx.AsyncBaseTransport | None = None
    jwks: JwksCache = field(init=False)

    def __post_init__(self) -> None:
        self.jwks = JwksCache(self._fetch)

    async def _fetch(self, url: str) -> Jwks:
        return await _fetch_jwks_with(self.transport, url)


def _client(provider: Provider, redirect_uri: str, transport: httpx.AsyncBaseTransport | None):
    return egress.google_oauth_client(
        client_id=provider.client_id,
        client_secret=provider.client_secret,
        scope=provider.scope,
        redirect_uri=redirect_uri,
        inner=transport,
    )


def _verifier(verifier: str) -> str:
    """PKCE is mandatory: a flow without a well-formed verifier never reaches Google."""
    if not isinstance(verifier, str) or VERIFIER.fullmatch(verifier) is None:
        raise ProviderError("no PKCE verifier")
    return verifier


async def authorization_url(
    provider: Provider,
    redirect_uri: str,
    *,
    state: str,
    nonce: str,
    verifier: str,
    reauth: bool,
) -> str:
    """The URL the browser goes to. Builds the URL only; nothing is sent."""
    extra: dict[str, str] = {"response_mode": "query", "nonce": nonce}
    if reauth:
        extra["max_age"] = "0"
        extra["claims"] = AUTH_TIME_CLAIMS
    async with _client(provider, redirect_uri, None) as client:
        url, _ = client.create_authorization_url(
            provider.authorize_url, state=state, code_verifier=_verifier(verifier), **extra
        )
    return url


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    subject: str
    email: str | None
    email_verified: bool
    auth_time: int | None


async def identify(
    runtime: OAuthRuntime,
    provider: Provider,
    redirect_uri: str,
    *,
    code: str,
    verifier: str,
    nonce: str | None,
) -> ProviderIdentity:
    """Exchange the code (with the flow's ``code_verifier``) and return who signed in. Raises
    ``ProviderError`` for any failure; nothing is sent without a verifier and a nonce."""
    if nonce is None:
        raise ProviderError("no nonce")
    verifier = _verifier(verifier)
    try:
        async with _client(provider, redirect_uri, runtime.transport) as client:
            token = await client.fetch_token(
                provider.token_url,
                code=code,
                code_verifier=verifier,
                grant_type="authorization_code",
                headers={"Accept": "application/json"},
            )
            id_token = token.get("id_token")
            if not isinstance(id_token, str):
                raise ProviderError("no ID token")
            claims: IdentityClaims = await verify_id_token(
                provider, id_token, nonce=nonce, cache=runtime.jwks
            )
            return ProviderIdentity(
                claims.subject, claims.email, claims.email_verified, claims.auth_time
            )
    except (IdTokenError, ProviderError) as exc:
        raise ProviderError(str(exc)) from None
    except OutboundBlocked:
        raise  # the worker's refusal keeps its own category (``outbound_blocked``), not "provider"
    except Exception as exc:  # httpx, Authlib: the text can hold URLs or bodies; keep the class
        raise ProviderError(f"provider call failed ({type(exc).__name__})") from None


__all__ = [
    "OAuthRuntime",
    "ProviderError",
    "ProviderIdentity",
    "authorization_url",
    "identify",
]
