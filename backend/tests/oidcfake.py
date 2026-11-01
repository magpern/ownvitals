"""An in-process fake of Google, Microsoft and GitHub (ADR 0003 section 11).

An httpx ``MockTransport`` with its own RSA signing keys and JWKS. ``complete(url, ...)`` plays
the user's browser at the provider: it reads the authorization URL our start endpoint returned
(checking state, nonce, PKCE and response_mode are present), registers a code and returns the
callback path. Google only includes ``auth_time`` when the request asked for it with the OIDC
``claims`` parameter. It can issue every bad token: wrong signature, ``none`` and HS256, wrong
``aud``/``iss`` (incl. Microsoft tenant mismatch), ``nonce``, ``auth_time``, expiry and
``iat``, and unverified or missing emails. Nothing here touches a network.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from joserfc import jwt
from joserfc.jwk import RSAKey

DROP = object()  # as a claim override: remove the claim
GOOGLE_JWKS = "https://www.googleapis.com/oauth2/v3/certs"
MICROSOFT_JWKS = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
TENANT = "tenant-1111"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _asks_for_auth_time(query: dict[str, list[str]]) -> bool:
    """True when the ``claims`` parameter requests ``auth_time`` in the ID token."""
    try:
        requested = json.loads(query.get("claims", ["{}"])[0])
        return "auth_time" in requested.get("id_token", {})
    except (ValueError, AttributeError, TypeError):
        return False


class FakeProviders:
    def __init__(
        self,
        client_ids: dict[str, str],
        secret: str = "fake-client-secret",  # noqa: S107
    ) -> None:
        self.client_ids = client_ids
        self.secret = secret
        self.key = RSAKey.generate_key(2048, parameters={"kid": "good-key"})
        self.other = RSAKey.generate_key(2048, parameters={"kid": "good-key"})  # same kid, bad key
        self.rotated = RSAKey.generate_key(2048, parameters={"kid": "rotated-key"})
        self.published: list[RSAKey] = [self.key]
        self.codes: dict[str, dict[str, Any]] = {}
        self.jwks_fetches = 0
        self.jwks_cache_control: str | None = None  # header the JWKS endpoints send
        self.jwks_status = 200
        self.token_requests = 0
        self.github_emails: dict[str, list[dict[str, Any]]] = {}
        self.transport = httpx.MockTransport(self._handle)
        self._counter = 0

    # ---- what the providers serve -------------------------------------------------------
    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in (GOOGLE_JWKS, MICROSOFT_JWKS):
            self.jwks_fetches += 1
            headers = {"Cache-Control": self.jwks_cache_control} if self.jwks_cache_control else {}
            return httpx.Response(
                self.jwks_status,
                json={"keys": [k.as_dict(private=False) for k in self.published]},
                headers=headers,
            )
        if request.method == "POST" and url.split("?")[0] in (
            "https://oauth2.googleapis.com/token",
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            "https://github.com/login/oauth/access_token",
        ):
            return self._token(request)
        if url.startswith("https://api.github.com/"):
            return self._github(request, url)
        return httpx.Response(404, json={"error": "unexpected request to " + request.url.host})

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests += 1
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        spec = self.codes.get(form.get("code", ""))
        verifier = form.get("code_verifier", "")
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        if (
            spec is None
            or form.get("client_secret") != self.secret
            or form.get("client_id") != spec["client_id"]
            or challenge != spec["challenge"]  # PKCE S256 must verify
            or form.get("redirect_uri") != spec["redirect_uri"]
        ):
            return httpx.Response(400, json={"error": "invalid_grant"})
        body: dict[str, Any] = {
            "access_token": spec.get("access_token", "at"),
            "token_type": "bearer",
        }
        if "id_token" in spec:
            body["id_token"] = spec["id_token"]
        return httpx.Response(200, json=body)

    def _github(self, request: httpx.Request, url: str) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if token not in self.github_emails:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if url.endswith("/user"):
            return httpx.Response(200, json={"id": int(token.split("-")[1]), "login": "octocat"})
        return httpx.Response(200, json=self.github_emails[token])

    # ---- token issuing ---------------------------------------------------------------------
    def sign(
        self,
        claims: dict[str, Any],
        *,
        alg: str = "RS256",
        key: RSAKey | None = None,
        kid: str | None = None,
    ) -> str:
        key = key or self.key
        header = {"alg": alg, "kid": kid or key.kid, "typ": "JWT"}
        if alg == "RS256":
            return jwt.encode(header, claims, key)
        signing_input = _b64(json.dumps(header).encode()) + "." + _b64(json.dumps(claims).encode())
        if alg == "none":
            return signing_input + "."
        # HS256 "key confusion": signed with public key bytes as the HMAC secret.
        secret = json.dumps(key.as_dict(private=False)).encode()
        mac = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
        return signing_input + "." + _b64(mac)

    def claims_for(self, provider: str, url: str, **overrides: Any) -> dict[str, Any]:
        query = parse_qs(urlsplit(url).query)
        now = int(time.time())
        claims: dict[str, Any] = {
            "aud": query["client_id"][0],
            "nonce": query["nonce"][0],
            "iat": now,
            "exp": now + 600,
            "auth_time": now,
            "sub": "sub-1",
        }
        if provider == "google":
            # Like the real Google: no auth_time unless the request asked for it (OIDC "claims").
            if not _asks_for_auth_time(query):
                claims.pop("auth_time")
            claims |= {
                "iss": "https://accounts.google.com",
                "email": "g@example.com",
                "email_verified": True,
            }
        else:
            claims |= {
                "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
                "tid": TENANT,
                "oid": "oid-1",
                "email": "m@example.com",
            }
        for name, value in overrides.items():
            if value is DROP:
                claims.pop(name, None)
            else:
                claims[name] = value
        return claims

    def complete(
        self,
        url: str,
        *,
        alg: str = "RS256",
        key: RSAKey | None = None,
        kid: str | None = None,
        state: str | None = None,
        github_user: int = 1,
        github_emails: list[dict[str, Any]] | None = None,
        **claims: Any,
    ) -> str:
        """The user approves at the provider: returns ``/oauth/<provider>/callback?code&state``."""
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        provider = {"accounts.google.com": "google", "github.com": "github"}.get(
            parts.netloc, "microsoft"
        )
        assert query["code_challenge_method"] == "S256"
        assert query["code_challenge"]
        assert query["state"]
        assert query["redirect_uri"].endswith(f"/oauth/{provider}/callback")
        self._counter += 1
        code = f"code-{self._counter}-{provider}"
        spec: dict[str, Any] = {
            "client_id": query["client_id"],
            "challenge": query["code_challenge"],
            "redirect_uri": query["redirect_uri"],
        }
        if provider == "github":
            assert "response_mode" not in query
            access = f"gh-{github_user}-{self._counter}"
            self.github_emails[access] = (
                github_emails
                if github_emails is not None
                else [{"email": "octo@example.com", "primary": True, "verified": True}]
            )
            spec["access_token"] = access
        else:
            assert query["response_mode"] == "query"
            assert query["nonce"]
            spec["id_token"] = self.sign(
                self.claims_for(provider, url, **claims), alg=alg, key=key, kid=kid
            )
        self.codes[code] = spec
        return f"/oauth/{provider}/callback?code={code}&state={state or query['state']}"
