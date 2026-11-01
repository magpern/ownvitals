"""ID-token validation (ADR 0003 section 5, "ID tokens (OIDC)").

RS256 only: ``none``, HS* and every other algorithm are refused before any key is looked at.
The signature is checked against the provider's JWKS. The keys are cached per URL for the
``Cache-Control: max-age`` the provider sent (default one hour when absent; at least a minute,
so ``0``, ``no-cache`` and ``no-store`` give a minute; at most 24 h),
so a key the provider retired stops being accepted; a token naming an unknown ``kid`` triggers
one refetch, at most once a minute per URL, and concurrent sign-ins share that fetch. Claims:
``iss``, ``aud`` (with ``azp`` equal to our client id when ``aud`` has several values), ``exp``,
``iat`` (60 s leeway) and ``nonce``. Microsoft's issuer must equal ``.../{tid}/v2.0`` for the
token's own ``tid`` and its subject is ``{tid}/{oid}``; its email is always unverified.
"""

import asyncio
import base64
import binascii
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from ownvitals.auth.oauth_providers import MICROSOFT, MICROSOFT_ISSUER, Provider

LEEWAY = 60  # seconds
JWKS_REFETCH_INTERVAL = 60.0  # seconds
JWKS_DEFAULT_TTL = 3600.0  # seconds, when the provider sends no max-age
JWKS_MIN_TTL = JWKS_REFETCH_INTERVAL
JWKS_MAX_TTL = 24 * 3600.0
ALGORITHM = "RS256"


class IdTokenError(Exception):
    """The ID token is not acceptable. The text names the check, never the token."""


@dataclass(frozen=True, slots=True)
class IdentityClaims:
    subject: str
    email: str | None
    email_verified: bool
    auth_time: int | None


@dataclass(frozen=True, slots=True)
class Jwks:
    """A fetched key set document and the ``max-age`` (seconds) the provider allowed, if any."""

    document: dict[str, Any]
    max_age: float | None = None


JwksFetcher = Callable[[str], Awaitable[Jwks]]


def _ttl(max_age: float | None) -> float:
    if max_age is None:
        return JWKS_DEFAULT_TTL
    return min(max(max_age, JWKS_MIN_TTL), JWKS_MAX_TTL)  # 0 (no-cache, no-store) -> the minimum


class JwksCache:
    """Keys per JWKS URL, kept for the provider's ``max-age`` (capped, see the module doc).

    A hit on a known ``kid`` never fetches; an unknown ``kid`` or an expired entry triggers one
    refetch, and no more than one per ``JWKS_REFETCH_INTERVAL`` for that URL. A per-URL lock
    makes concurrent requests for the same new ``kid`` share one fetch: the others re-check the
    cache once they hold the lock instead of being refused by the rate limit.
    """

    def __init__(self, fetch: JwksFetcher, clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._clock = clock
        self._keys: dict[str, KeySet] = {}
        self._expires_at: dict[str, float] = {}
        self._fetched_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _known(self, url: str, kid: str) -> bool:
        keys = self._keys.get(url)
        if keys is None or self._clock() >= self._expires_at[url]:
            return False
        return any(key.kid == kid for key in keys.keys)

    async def key_set(self, url: str, kid: str) -> KeySet:
        if self._known(url, kid):
            return self._keys[url]
        async with self._locks.setdefault(url, asyncio.Lock()):
            if self._known(url, kid):  # another request fetched it while this one waited
                return self._keys[url]
            last = self._fetched_at.get(url)
            if last is not None and self._clock() - last < JWKS_REFETCH_INTERVAL:
                raise IdTokenError("unknown key id (refetch rate limit)")
            try:
                fetched = await self._fetch(url)
                keys = KeySet.import_key_set(fetched.document)  # pyright: ignore[reportArgumentType]
            except Exception:
                # Unexpired keys stay: an unknown kid (anyone can send one) must not turn
                # sign-in off during a provider outage. Expired ones are never served.
                raise IdTokenError("the signing keys could not be fetched") from None
            finally:
                self._fetched_at[url] = self._clock()  # counted when the fetch is over, also failed
            self._keys[url] = keys
            self._expires_at[url] = self._clock() + _ttl(fetched.max_age)
            if not self._known(url, kid):
                raise IdTokenError("unknown key id")
            return keys


def _header(token: str) -> dict[str, Any]:
    try:
        raw = token.split(".")[0]
        header = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, binascii.Error):
        raise IdTokenError("malformed token") from None
    if not isinstance(header, dict):
        raise IdTokenError("malformed token")
    return header  # pyright: ignore[reportUnknownVariableType]


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise IdTokenError(f"{name} is not a number")
    return float(value)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


async def verify_id_token(
    provider: Provider,
    token: str,
    *,
    nonce: str,
    cache: JwksCache,
    now: float | None = None,
) -> IdentityClaims:
    if provider.jwks_url is None:
        raise IdTokenError("not an OIDC provider")
    now = time.time() if now is None else now
    header = _header(token)
    if header.get("alg") != ALGORITHM:
        raise IdTokenError("algorithm is not RS256")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise IdTokenError("missing key id")
    keys = await cache.key_set(provider.jwks_url, kid)
    try:
        claims: dict[str, Any] = dict(jwt.decode(token, keys, algorithms=[ALGORITHM]).claims)
    except (JoseError, ValueError, KeyError):
        raise IdTokenError("bad signature") from None

    exp, iat = _number(claims.get("exp"), "exp"), _number(claims.get("iat"), "iat")
    if exp + LEEWAY < now:
        raise IdTokenError("expired")
    if iat - LEEWAY > now:
        raise IdTokenError("issued in the future")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if provider.client_id not in audiences:
        raise IdTokenError("wrong audience")
    if len(audiences) > 1 and claims.get("azp") != provider.client_id:
        raise IdTokenError("authorized party is not this client")  # OIDC Core 3.1.3.7 rule 4
    supplied = claims.get("nonce")
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), nonce.encode()):
        raise IdTokenError("nonce mismatch")
    raw_auth_time = claims.get("auth_time")
    auth_time = int(_number(raw_auth_time, "auth_time")) if raw_auth_time is not None else None
    issuer, email = claims.get("iss"), _text(claims.get("email"))

    if provider.name == MICROSOFT:
        tid, oid = _text(claims.get("tid")), _text(claims.get("oid"))
        if tid is None or oid is None:
            raise IdTokenError("missing tid or oid")
        if issuer != MICROSOFT_ISSUER.format(tid=tid):
            raise IdTokenError("issuer does not match the tenant")
        # Anyone can create a tenant claiming any address: never verified.
        return IdentityClaims(f"{tid}/{oid}", email, False, auth_time)

    if issuer not in provider.issuers:
        raise IdTokenError("wrong issuer")
    subject = _text(claims.get("sub"))
    if subject is None:
        raise IdTokenError("missing subject")
    verified = claims.get("email_verified") in (True, "true")
    return IdentityClaims(subject, email, email is not None and verified, auth_time)
