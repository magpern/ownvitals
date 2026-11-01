"""Derived keys (ADR 0003 section 1, "Derived keys"; classbeat ADR 0003 section 8).

One secret file, ``app_secret_key``, feeds HKDF-SHA256 (fixed salt ``ownvitals-v1``); each use
gets its own 32-byte key through its own info string, so a key for one purpose is useless for
another. Rotating ``app_secret_key`` invalidates CSRF tokens, rate-limit buckets, known devices
(the ``device`` key: the cookies stop matching, so every sign-in looks like a new device and
mails the user once more) and recovery codes. It never touches TOTP seeds: they are on the
envelope scheme of ADR 0004 from the first enrolment (ADR 0003 section 5, ``totp_keys``), so
classbeat's ``totp-v1`` key and its ``app-v1`` form are not ported.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ownvitals.config import MIN_APP_SECRET_BYTES, Settings

HKDF_SALT = b"ownvitals-v1"
KEY_LENGTH = 32
NONCE_LENGTH = 12  # 96 bits


def derive_key(secret: bytes, info: str) -> bytes:
    """A 32-byte key for ``info`` (``csrf``, ``rate-limit``, ``device``, ...)."""
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_LENGTH, salt=HKDF_SALT, info=info.encode()
    ).derive(secret)


@dataclass(frozen=True, slots=True, repr=False)
class Keys:
    """The five derived keys. ``repr`` never shows key material."""

    csrf: bytes
    rate_limit: bytes
    device: bytes
    recovery_codes: bytes
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
