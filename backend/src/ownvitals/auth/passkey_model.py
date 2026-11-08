"""Passkeys: the values, the failure type and the parsing that need no database (ADR 0011
sections 1 to 4).

Pure: no database, no clock and no randomness. ``passkeys`` re-exports these names.
"""

import base64
import binascii
import json
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

TIMEOUT_MS = 120_000
CHALLENGE_BYTES = 32
CHALLENGE_LIFETIME = timedelta(minutes=5)
HANDLE_BYTES = 32
CREDENTIAL_ID_MAX = 1023
NAME_MAX = 64
DEFAULT_NAME = "Passkey"
COUNTER_MAX = 2**32 - 1

Purpose = Literal["register", "signin", "reauth"]
# The closed set of ``detail.reason`` values of ``passkey_signin_failed`` and friends.
REASONS = frozenset(
    {
        "client_data",
        "challenge",
        "unknown_credential",
        "user_handle",
        "suspended",
        "account",
        "no_totp",
        "verification",
        "sign_count",
        "race",
        "duplicate",
        "limit",
        "session",
    }
)


class PasskeyFailure(Exception):
    """A refused ceremony. The route answers one generic failure whatever the ``reason``;
    the reason goes only into ``auth_events``. ``suspended`` is ``(passkey name, account
    email)`` when this failure suspended the credential, for the notice mail."""

    def __init__(
        self,
        reason: str,
        *,
        user_id: UUID | None = None,
        suspended: tuple[str, str] | None = None,
    ) -> None:
        if reason not in REASONS:
            raise ValueError("unknown passkey failure reason")
        super().__init__(reason)
        self.reason = reason
        self.user_id = user_id
        self.suspended = suspended


@dataclass(frozen=True, slots=True)
class RelyingParty:
    rp_id: str
    origin: str


@dataclass(frozen=True, slots=True)
class Passkey:
    """What the account page shows. Never the credential id, the key or the counter."""

    id: UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None
    backup_eligible: bool
    backup_state: bool
    suspended: bool


def normalize_name(raw: str | None) -> str:
    """NFKC, control and format characters removed, trimmed; 1 to 64 characters. An empty
    or missing name is "Passkey". ``ValueError`` when it is longer."""
    text = unicodedata.normalize("NFKC", raw or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))
    text = " ".join(text.split())
    if not text:
        return DEFAULT_NAME
    if len(text) > NAME_MAX:
        raise ValueError("name too long")
    return text


def b64url_decode(value: str) -> bytes:
    """Strict base64url (padding optional). ``ValueError`` on anything else."""
    if any(ch not in _B64URL for ch in value.rstrip("=")):
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(value.rstrip("=") + "=" * (-len(value.rstrip("=")) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("not base64url") from None


_B64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def read_client_data(encoded: str, expected_type: str, rp: RelyingParty) -> bytes:
    """The challenge of a ``clientDataJSON`` whose type and origin are the expected ones and
    which was not made in a cross-origin frame. ``PasskeyFailure("client_data")`` otherwise."""
    try:
        data = json.loads(b64url_decode(encoded))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        challenge = data.get("challenge")
        if (
            data.get("type") != expected_type
            or data.get("origin") != rp.origin
            or data.get("crossOrigin") not in (None, False)
            or not isinstance(challenge, str)
        ):
            raise ValueError("unexpected client data")
        decoded = b64url_decode(challenge)
    except (ValueError, UnicodeDecodeError):
        raise PasskeyFailure("client_data") from None
    if len(decoded) != CHALLENGE_BYTES:
        raise PasskeyFailure("client_data")
    return decoded


def is_cross_device(attachment: str | None, transports: Collection[str]) -> bool:
    """Whether a sign-in gets the shorter cross-device session (ADR 0011 section 3, amendment
    of 2026-11-08). ``attachment`` is the assertion's ``authenticatorAttachment``; an
    assertion carries no transports, so ``transports`` are those the browser listed when the
    credential was registered.

    Both are client-reported and unsigned, so they only ever make the session stricter. It is a
    same-device sign-in only when the browser says ``platform`` **and** the credential was
    registered with the ``internal`` transport (this device's own authenticator). Anything else
    is cross-device: ``cross-platform`` (a phone over the hybrid transport, a security key), a
    missing attachment, or a ``platform`` claim the transports contradict (a credential
    registered without ``internal``, or with no transports at all)."""
    return not (attachment == "platform" and "internal" in transports)
