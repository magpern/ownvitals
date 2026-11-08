"""A software WebAuthn authenticator and passkey flows for the tests (ADR 0011).

Synthetic only: every key is generated in the test process and never leaves it; no real
device, browser or account is involved. ``SoftAuthenticator`` builds the JSON a browser's
``navigator.credentials.create()`` and ``get()`` return (``attestation: none``), and can break
each part a test needs broken: the origin, the type, the challenge, the ``rp.id`` hash, the UP
and UV flags, the algorithm, the signature, the counter and the user handle.
"""

import base64
import hashlib
import json
import secrets
import struct
from dataclasses import dataclass, field
from typing import Any, Literal

import cbor2
import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from authtools import ORIGIN
from flows import Browser

RP_ID = "ownvitals.test"  # the host of authtools.ORIGIN
UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40
Algorithm = Literal["es256", "ed25519", "es384"]


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class SoftCredential:
    credential_id: bytes
    key: Any
    algorithm: Algorithm
    user_handle: bytes
    sign_count: int = 0

    def cose(self) -> bytes:
        if self.algorithm == "ed25519":
            raw = self.key.public_key().public_bytes_raw()
            return cbor2.dumps({1: 1, 3: -8, -1: 6, -2: raw})
        numbers = self.key.public_key().public_numbers()
        size = 32 if self.algorithm == "es256" else 48
        alg, crv = (-7, 1) if self.algorithm == "es256" else (-35, 2)
        return cbor2.dumps(
            {
                1: 2,
                3: alg,
                -1: crv,
                -2: numbers.x.to_bytes(size, "big"),
                -3: numbers.y.to_bytes(size, "big"),
            }
        )

    def sign(self, data: bytes) -> bytes:
        if self.algorithm == "ed25519":
            return self.key.sign(data)
        digest = hashes.SHA256() if self.algorithm == "es256" else hashes.SHA384()
        return self.key.sign(data, ec.ECDSA(digest))


def _new_key(algorithm: Algorithm) -> Any:
    if algorithm == "ed25519":
        return ed25519.Ed25519PrivateKey.generate()
    return ec.generate_private_key(ec.SECP256R1() if algorithm == "es256" else ec.SECP384R1())


@dataclass
class SoftAuthenticator:
    """One synthetic authenticator holding any number of discoverable credentials."""

    rp_id: str = RP_ID
    origin: str = ORIGIN
    credentials: list[SoftCredential] = field(default_factory=lambda: list[SoftCredential]())
    counter_step: int = 1  # 0: a synced passkey that never counts

    def _client_data(
        self, kind: str, challenge: str, origin: str | None, cross_origin: bool | None
    ) -> bytes:
        data: dict[str, Any] = {
            "type": kind,
            "challenge": challenge,
            "origin": origin or self.origin,
        }
        if cross_origin is not None:
            data["crossOrigin"] = cross_origin
        return json.dumps(data).encode()

    def create(
        self,
        options: dict[str, Any],
        *,
        algorithm: Algorithm = "es256",
        origin: str | None = None,
        rp_id: str | None = None,
        kind: str = "webauthn.create",
        challenge: str | None = None,
        flags: int = UP | UV | AT,
        cross_origin: bool | None = False,
        credential_id: bytes | None = None,
        sign_count: int = 0,
        fmt: Literal["none", "packed"] = "none",
        transports: list[str] | None = None,
    ) -> dict[str, Any]:
        """The response to registration ``options`` (as the server sent them). ``fmt`` packed
        gives a valid self-attestation (signed by the credential's own key)."""
        credential = SoftCredential(
            credential_id or secrets.token_bytes(32),
            _new_key(algorithm),
            algorithm,
            unb64(options["user"]["id"]),
            sign_count,
        )
        auth_data = (
            hashlib.sha256((rp_id or self.rp_id).encode()).digest()
            + bytes([flags])
            + struct.pack(">I", sign_count)
            + bytes(16)  # aaguid: all zeros, as attestation "none" may report
            + struct.pack(">H", len(credential.credential_id))
            + credential.credential_id
            + credential.cose()
        )
        client_data = self._client_data(
            kind, challenge or options["challenge"], origin, cross_origin
        )
        statement: dict[str, Any] = {}
        if fmt == "packed":
            signed = auth_data + hashlib.sha256(client_data).digest()
            statement = {"alg": -7, "sig": credential.sign(signed)}
        attestation = cbor2.dumps({"fmt": fmt, "attStmt": statement, "authData": auth_data})
        self.credentials.append(credential)
        return {
            "id": b64(credential.credential_id),
            "rawId": b64(credential.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "attestationObject": b64(attestation),
                "transports": ["internal", "hybrid"] if transports is None else transports,
            },
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
        }

    def get(
        self,
        options: dict[str, Any],
        *,
        credential: SoftCredential | None = None,
        origin: str | None = None,
        rp_id: str | None = None,
        kind: str = "webauthn.get",
        challenge: str | None = None,
        flags: int = UP | UV,
        cross_origin: bool | None = False,
        sign_count: int | None = None,
        user_handle: bytes | Literal["keep"] | None = "keep",
        bad_signature: bool = False,
        attachment: str | None = "platform",
    ) -> dict[str, Any]:
        """An assertion for ``options`` with ``credential`` (default: the newest).
        ``attachment`` is the client-reported ``authenticatorAttachment`` (``None``: absent)."""
        chosen = credential or self.credentials[-1]
        if sign_count is None:
            chosen.sign_count += self.counter_step
            sign_count = chosen.sign_count
        auth_data = (
            hashlib.sha256((rp_id or self.rp_id).encode()).digest()
            + bytes([flags])
            + struct.pack(">I", sign_count)
        )
        client_data = self._client_data(
            kind, challenge or options["challenge"], origin, cross_origin
        )
        signature = chosen.sign(auth_data + hashlib.sha256(client_data).digest())
        if bad_signature:
            signature = chosen.sign(auth_data + hashlib.sha256(b"something else").digest())
        response: dict[str, Any] = {
            "clientDataJSON": b64(client_data),
            "authenticatorData": b64(auth_data),
            "signature": b64(signature),
        }
        handle = chosen.user_handle if user_handle == "keep" else user_handle
        if handle is not None:
            response["userHandle"] = b64(handle)
        answer: dict[str, Any] = {
            "id": b64(chosen.credential_id),
            "rawId": b64(chosen.credential_id),
            "type": "public-key",
            "response": response,
            "clientExtensionResults": {},
        }
        if attachment is not None:
            answer["authenticatorAttachment"] = attachment
        return answer


# --- flows through the real routes ----------------------------------------------------------------

BASE = "/api/auth/passkeys"


async def register_options(browser: Browser) -> dict[str, Any]:
    answer = await browser.post(f"{BASE}/register/options")
    assert answer.status_code == 200, answer.text
    return answer.json()


async def register(
    browser: Browser, auth: SoftAuthenticator, name: str | None = None, **kwargs: Any
) -> httpx.Response:
    """Registration options, the authenticator's answer, and the registration."""
    options = await register_options(browser)
    body: dict[str, Any] = {"credential": auth.create(options, **kwargs)}
    if name is not None:
        body["name"] = name
    return await browser.post(f"{BASE}/register", body)


async def signin_options(browser: Browser) -> dict[str, Any]:
    answer = await browser.post(f"{BASE}/signin/options")
    assert answer.status_code == 200, answer.text
    return answer.json()


async def sign_in(browser: Browser, auth: SoftAuthenticator, **kwargs: Any) -> httpx.Response:
    options = await signin_options(browser)
    return await browser.post(f"{BASE}/signin", {"credential": auth.get(options, **kwargs)})


async def reauth(browser: Browser, auth: SoftAuthenticator, **kwargs: Any) -> httpx.Response:
    answer = await browser.post(f"{BASE}/reauth/options")
    assert answer.status_code == 200, answer.text
    return await browser.post(f"{BASE}/reauth", {"credential": auth.get(answer.json(), **kwargs)})


def failed(response: httpx.Response) -> bool:
    """The one generic answer of every refused passkey ceremony."""
    return response.status_code == 401 and response.json() == {
        "detail": {"reason": "passkey_failed"}
    }
