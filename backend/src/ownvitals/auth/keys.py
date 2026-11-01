"""Derived keys and the AES-GCM helper for TOTP secrets (ADR 0003 section 8).

One secret file, ``app_secret_key``, feeds HKDF-SHA256 (fixed salt ``ownvitals-v1``); each use
gets its own 32-byte key through its own info string, so a key for one purpose is useless for
another. Rotating ``app_secret_key`` invalidates CSRF tokens, rate-limit buckets, known devices
(the ``device`` key: the cookies stop matching, so every sign-in looks like a new device and
mails the user once more) and recovery codes; TOTP secrets then need re-enrolling until M2's
envelope encryption takes them over.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ownvitals.config import MIN_APP_SECRET_BYTES, Settings

HKDF_SALT = b"ownvitals-v1"
KEY_LENGTH = 32
TOTP_KEY_ID = "app-v1"
NONCE_LENGTH = 12  # 96 bits


def derive_key(secret: bytes, info: str) -> bytes:
    """A 32-byte key for ``info`` (``csrf``, ``rate-limit``, ``device``, ...)."""
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_LENGTH, salt=HKDF_SALT, info=info.encode()
    ).derive(secret)


@dataclass(frozen=True, slots=True, repr=False)
class Keys:
    """The six derived keys. ``repr`` never shows key material."""

    csrf: bytes
    rate_limit: bytes
    device: bytes
    recovery_codes: bytes
    totp: bytes
    totp_pending: bytes

    def __repr__(self) -> str:
        return "Keys(...)"

    @classmethod
    def from_secret(cls, secret: bytes) -> "Keys":
        if len(secret) < MIN_APP_SECRET_BYTES:
            raise ValueError(f"app_secret_key must be at least {MIN_APP_SECRET_BYTES} bytes long")
        return cls(
            csrf=derive_key(secret, "csrf"),
            rate_limit=derive_key(secret, "rate-limit"),
            device=derive_key(secret, "device"),
            recovery_codes=derive_key(secret, "recovery-codes"),
            totp=derive_key(secret, "totp-v1"),
            totp_pending=derive_key(secret, "totp-pending"),
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "Keys":
        if settings.app_secret_key is None:
            raise RuntimeError(
                "the app_secret_key secret file is missing "
                "(scripts/worktree.sh init creates it; see ADR 0003 section 8)"
            )
        return cls.from_secret(settings.app_secret_key.get_secret_value().encode())


def hmac_digest(key: bytes, *parts: str) -> bytes:
    """HMAC-SHA256 over ``parts``, joined unambiguously (NUL cannot occur in any of ours)."""
    return hmac.new(key, "\x00".join(parts).encode(), hashlib.sha256).digest()


def token_hash(token: str) -> bytes:
    """SHA-256 of a server-issued random token; the only form that is stored."""
    return hashlib.sha256(token.encode()).digest()


def new_token() -> str:
    """32 random bytes, URL-safe."""
    return secrets.token_urlsafe(32)


def _totp_aad(user_id: UUID) -> bytes:
    return f"{user_id}|totp".encode()


def encrypt_totp_secret(keys: Keys, user_id: UUID, plaintext: bytes) -> tuple[bytes, bytes, str]:
    """``(ciphertext, nonce, key_id)``: AES-256-GCM, a fresh random nonce, AAD ``user_id|totp``."""
    nonce = secrets.token_bytes(NONCE_LENGTH)
    ciphertext = AESGCM(keys.totp).encrypt(nonce, plaintext, _totp_aad(user_id))
    return ciphertext, nonce, TOTP_KEY_ID


def decrypt_totp_secret(
    keys: Keys, user_id: UUID, ciphertext: bytes, nonce: bytes, key_id: str
) -> bytes:
    """Raises ``ValueError`` for another key scheme, or for a secret that is not this user's."""
    if key_id != TOTP_KEY_ID:
        raise ValueError(f"unknown key_id {key_id!r}")
    try:
        return AESGCM(keys.totp).decrypt(nonce, ciphertext, _totp_aad(user_id))
    except InvalidTag:
        raise ValueError("the TOTP secret does not decrypt for this user") from None
