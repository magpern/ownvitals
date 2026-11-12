"""The signed one-click unsubscribe token (ADR 0017; #459). Pure: no clock, no I/O.

A token binds one user, one suppressible kind, the generation of that kind's preference when the
mail was sent, and an expiry; it is MAC'd with a key derived for this purpose only:
HKDF-SHA256 of ``app_secret_key`` with the label ``unsubscribe-v1`` (``ownvitals.auth.keys``,
the same derivation as every other app key, ADR 0003 section 1), so it is useless for anything
else and no other key verifies it. Rotating ``app_secret_key`` voids every link already sent.

Layout, then URL-safe base64 without padding (67 characters)::

    version (1) | owner id (16) | kind index (1) | generation (8) | expires, unix s (8) | mac (16)

The MAC is HMAC-SHA256 under the derived key over ``DOMAIN`` and the 34 bytes before it,
truncated to 128 bits, compared in constant time. ``verify`` returns the claim only for a token
that is well formed, authentic and not expired; every refusal is the same ``None``, so a caller
cannot tell a forged token from an expired one. Whether the generation is still current (a
replay after a change) is the database's check, inside ``email_unsubscribe``.

The token is never stored and never logged: the sender builds it at send time into the mail
only, and the endpoint reads it from the query string, which the access log never records.
"""

import base64
import binascii
import hmac
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Final
from uuid import UUID

from ownvitals.auth.keys import derive_key
from ownvitals.notify.email_policy import SUPPRESSIBLE_KINDS

LABEL: Final = "unsubscribe-v1"
DOMAIN: Final = b"ownvitals-unsubscribe\x00"
VERSION: Final = 1
LIFETIME: Final = timedelta(days=60)
# The endpoint's path: the link in a mail and the ``List-Unsubscribe`` header are
# ``public_base_url`` plus this and ``?t=`` plus the token.
PATH: Final = "/api/unsubscribe"
QUERY: Final = "t"

_PAYLOAD = struct.Struct(">B16sBQQ")
_MAC_BYTES: Final = 16
TOKEN_BYTES: Final = _PAYLOAD.size + _MAC_BYTES
TOKEN_CHARS: Final = 67
TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{67}")
MAX_GENERATION: Final = 2**63 - 1  # the column is a bigint


@dataclass(frozen=True, slots=True, repr=False)
class Claim:
    """What an authentic, unexpired token says. ``repr`` shows none of it."""

    owner_id: UUID
    kind: str
    generation: int

    def __repr__(self) -> str:
        return "<unsubscribe claim>"


def unsubscribe_key(secret: bytes) -> bytes:
    """The token key, derived from ``app_secret_key`` for this purpose only."""
    return derive_key(secret, LABEL)


def _mac(key: bytes, payload: bytes) -> bytes:
    return hmac.new(key, DOMAIN + payload, sha256).digest()[:_MAC_BYTES]


def issue(key: bytes, owner_id: UUID, kind: str, generation: int, now: datetime) -> str:
    """A token for ``owner_id``'s ``kind`` at ``generation``, valid for ``LIFETIME`` from
    ``now`` (an aware datetime). ``ValueError`` for a kind that is not suppressible."""
    if kind not in SUPPRESSIBLE_KINDS:
        raise ValueError("not a suppressible mail kind")
    if not 0 <= generation <= MAX_GENERATION:
        raise ValueError("generation out of range")
    expires = int((now + LIFETIME).timestamp())
    payload = _PAYLOAD.pack(
        VERSION, owner_id.bytes, SUPPRESSIBLE_KINDS.index(kind), generation, expires
    )
    raw = payload + _mac(key, payload)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def verify(key: bytes, token: object, now: datetime) -> Claim | None:
    """The claim of an authentic token that has not expired at ``now``; otherwise ``None``,
    whatever the reason."""
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=")
    except (binascii.Error, ValueError):
        return None
    if len(raw) != TOKEN_BYTES:
        return None
    payload, mac = raw[: _PAYLOAD.size], raw[_PAYLOAD.size :]
    if not hmac.compare_digest(mac, _mac(key, payload)):
        return None
    version, owner, kind_index, generation, expires = _PAYLOAD.unpack(payload)
    if version != VERSION or kind_index >= len(SUPPRESSIBLE_KINDS):
        return None
    if generation > MAX_GENERATION or now.timestamp() >= expires:
        return None
    return Claim(
        owner_id=UUID(bytes=owner), kind=SUPPRESSIBLE_KINDS[kind_index], generation=generation
    )


def link(public_base_url: str, token: str) -> str:
    """The unsubscribe URL of ``token``: the endpoint under ``public_base_url`` (T12)."""
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("not an unsubscribe token")
    return f"{public_base_url.rstrip('/')}{PATH}?{QUERY}={token}"
