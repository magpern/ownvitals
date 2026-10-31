"""Envelope encryption: AES-256-GCM payloads under a per-write data key, wrapped by a KEK.

ADR 0004 section 2. Pure: no database, no files, no logging. Every encryption, decryption, wrap
and unwrap of ownvitals goes through here. Only ``ownvitals.connections``, ``ownvitals.auth``
(TOTP) and ``ownvitals.crypto.keks`` (the key ring) may import it (``tests/test_crypto_guard.py``).

- A **data key** (DEK, 32 random bytes) is new for every write and encrypts exactly one payload
  with a random 96-bit nonce, so nonce reuse under one key cannot happen.
- The DEK is **wrapped** with AES-256-GCM under the KEK named by ``key_id``, its own random
  nonce and AAD that names the key and the row. A wrapped DEK is 48 bytes (32 + the GCM tag).
- AAD strings start with a version prefix per kind of value and join fields with ``|``; no field
  can contain one (UUIDs in canonical text form, provider names and key ids from their
  patterns), so they cannot collide. The two AADs of a value are **bound**: the wrap AAD names
  the id of the KEK that wraps or unwraps the DEK, and the payload AAD names the same value, of
  the matching kind. A mismatch is a ``ValueError`` when writing and ``DecryptFailed`` when
  reading. Nonces are exactly 12 bytes.
- The ring holds three **kinds** of key (``KeyKind``): KEKs, and the webhook-secret seed and
  pepper of ADR 0004 section 4. Each kind has its own id prefix, its own class and its own check
  message, so a key of one kind can't stand in for another: only a ``Kek`` wraps a data key.
- A **retired** KEK (``connection_kek_<id>``) is a ``RetiredKek``: decrypt only (ADR 0004
  section 3). It unwraps and opens; ``wrap``, ``seal`` and ``rewrap``'s target refuse it with
  ``KeyRetired``.

Any authentication failure raises ``DecryptFailed`` with no detail, and nothing else is tried.
"""

import contextlib
import enum
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from typing import Any, ClassVar, NoReturn
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_LENGTH = 32
NONCE_LENGTH = 12  # 96 bits
TAG_LENGTH = 16
WRAPPED_DEK_LENGTH = KEY_LENGTH + TAG_LENGTH
CHECK_VALUE_LENGTH = 16
# Stored bytes as a driver may hand ``bytea`` back.
type Stored = bytes | memoryview
# A provider name in an AAD (``withings``; #13 adds its test-only provider): no separator.
PROVIDER_PATTERN = re.compile(r"[a-z][a-z0-9]{0,31}")


class KeyKind(enum.Enum):
    """The kinds of key in the ring; the value is the id prefix (ADR 0004 sections 3 and 4)."""

    KEK = "kek"  # wraps data keys
    SEED = "seed"  # webhook secrets are derived from it (worker only)
    PEPPER = "pep"  # webhook-secret HMACs are made with it (web and worker)

    @property
    def id_pattern(self) -> re.Pattern[str]:
        """``^<prefix>-[a-z0-9-]{1,28}$``, used with ``fullmatch``."""
        return _ID_PATTERNS[self]

    @property
    def check_message(self) -> bytes:
        return _CHECK_MESSAGES[self]


_ID_PATTERNS = {kind: re.compile(rf"{kind.value}-[a-z0-9-]{{1,28}}") for kind in KeyKind}
_CHECK_MESSAGES = {
    KeyKind.KEK: b"ownvitals-kek-check-v1",
    KeyKind.SEED: b"ownvitals-seed-check-v1",
    KeyKind.PEPPER: b"ownvitals-pepper-check-v1",
}


class DecryptFailed(Exception):
    """A GCM authentication failure (wrong key, AAD or tampered bytes). Carries no detail."""

    def __init__(self) -> None:
        super().__init__("decrypt_failed")


class KeyRetired(Exception):
    """A retired KEK was handed to ``wrap``, ``seal`` or ``rewrap``'s target: retired keys are
    decrypt only (ADR 0004 section 3)."""

    def __init__(self) -> None:
        super().__init__("key_retired")


def is_key_id(value: object, kind: KeyKind = KeyKind.KEK) -> bool:
    """Whether ``value`` is an id of ``kind`` (for a KEK ``^kek-[a-z0-9-]{1,28}$``)."""
    return isinstance(value, str) and kind.id_pattern.fullmatch(value) is not None


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class RingKey:
    """A key of the ring and its id; only its subclasses are built. ``repr`` never shows the
    key, and it is never pickled or copied."""

    KIND: ClassVar[KeyKind]
    key_id: str
    key: bytes

    def __post_init__(self) -> None:
        if type(self) is RingKey:
            raise TypeError("a ring key is a Kek, a WebhookSeed or a WebhookPepper")
        if not is_key_id(self.key_id, self.KIND):
            raise ValueError(f"the key id does not match ^{self.KIND.id_pattern.pattern}$")
        if not isinstance(self.key, bytes) or len(self.key) != KEY_LENGTH:
            raise ValueError("a ring key is exactly 32 bytes")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(key_id={self.key_id!r}, key=...)"

    def __reduce__(self) -> NoReturn:
        raise TypeError("a ring key cannot be pickled or copied")


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class Kek(RingKey):
    """A key-encryption key (``kek-…``): the only kind that wraps a data key."""

    KIND: ClassVar[KeyKind] = KeyKind.KEK


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class RetiredKek(RingKey):
    """A retired KEK (``connection_kek_<id>``, ``kek-…``): decrypt only (ADR 0004 section 3). It
    unwraps and opens what it wrapped; it never wraps, seals or is ``rewrap``'s target."""

    KIND: ClassVar[KeyKind] = KeyKind.KEK


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class WebhookSeed(RingKey):
    """The webhook-secret seed (``seed-…``, ADR 0004 section 4). Only the worker loads one."""

    KIND: ClassVar[KeyKind] = KeyKind.SEED


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class WebhookPepper(RingKey):
    """The webhook-secret pepper (``pep-…``, ADR 0004 section 4). It never encrypts anything."""

    KIND: ClassVar[KeyKind] = KeyKind.PEPPER


@dataclass(frozen=True, slots=True)
class Sealed:
    """What is stored: the payload ciphertext and nonce, and the wrapped DEK and its nonce."""

    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    dek_nonce: bytes
    key_id: str

    def __repr__(self) -> str:
        return f"Sealed(key_id={self.key_id!r}, ...)"


def check_value(key: RingKey) -> bytes:
    """``HMAC-SHA256(key, "ownvitals-<kind>-check-v1")``, first 16 bytes: the ``connection_keks``
    row that detects a different key under an unchanged id (ADR 0004 sections 3 and 4)."""
    return hmac.new(key.key, key.KIND.check_message, hashlib.sha256).digest()[:CHECK_VALUE_LENGTH]


def canonical_json(obj: Any) -> bytes:
    """The one JSON encoding of a payload."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _uuid(value: UUID) -> str:
    if not isinstance(value, UUID):
        raise TypeError("a UUID is required")
    return str(value)  # canonical: lower case, hyphenated, 36 characters


def _provider(value: str) -> str:
    if not isinstance(value, str) or PROVIDER_PATTERN.fullmatch(value) is None:
        raise ValueError("the provider does not match ^[a-z][a-z0-9]{0,31}$")
    return value


def _key_id(value: str) -> str:
    if not is_key_id(value, KeyKind.KEK):
        raise ValueError("the KEK id does not match ^kek-[a-z0-9-]{1,28}$")
    return value


def connection_aad(owner_id: UUID, provider: str, row_id: UUID) -> bytes:
    """``ownvitals-conn-v1|<owner_id>|<provider>|<id>``: the payload of a connection row."""
    return f"ownvitals-conn-v1|{_uuid(owner_id)}|{_provider(provider)}|{_uuid(row_id)}".encode()


def connection_wrap_aad(key_id: str, owner_id: UUID, provider: str, row_id: UUID) -> bytes:
    """``ownvitals-dek-v1|<key_id>|<owner_id>|<provider>|<id>``: the wrapped DEK of that row."""
    return (
        f"ownvitals-dek-v1|{_key_id(key_id)}|{_uuid(owner_id)}|{_provider(provider)}"
        f"|{_uuid(row_id)}"
    ).encode()


def totp_aad(user_id: UUID) -> bytes:
    """``ownvitals-totp-v1|<user_id>``: a TOTP seed (ADR 0003 section 5)."""
    return f"ownvitals-totp-v1|{_uuid(user_id)}".encode()


def totp_wrap_aad(key_id: str, user_id: UUID) -> bytes:
    """``ownvitals-totp-dek-v1|<key_id>|<user_id>``: the wrapped DEK of a TOTP seed."""
    return f"ownvitals-totp-dek-v1|{_key_id(key_id)}|{_uuid(user_id)}".encode()


# The prefix of each kind of wrap AAD -> the payload AAD prefix of the same kind of value.
_PAYLOAD_PREFIX_OF_WRAP = {
    b"ownvitals-dek-v1": b"ownvitals-conn-v1",
    b"ownvitals-totp-dek-v1": b"ownvitals-totp-v1",
}


def _writing(kek: Kek) -> None:
    """Only a current KEK writes; a retired one is ``KeyRetired``."""
    if type(kek) is RetiredKek:
        raise KeyRetired
    if type(kek) is not Kek:
        raise TypeError("only a KEK wraps a data key")


def _opening(kek: Kek | RetiredKek) -> None:
    if type(kek) is not Kek and type(kek) is not RetiredKek:
        raise TypeError("only a KEK unwraps a data key")


def _wrap_fields(wrap_aad: bytes) -> list[bytes] | None:
    """``[prefix, key_id, *value]`` of a wrap AAD of a known kind, else ``None``."""
    fields = wrap_aad.split(b"|")
    if len(fields) < 3 or fields[0] not in _PAYLOAD_PREFIX_OF_WRAP:
        return None
    return fields


def _names_kek(wrap_aad: bytes, kek: RingKey) -> bool:
    """Whether ``wrap_aad`` is a wrap AAD of a known kind naming ``kek``'s id."""
    fields = _wrap_fields(wrap_aad)
    return fields is not None and fields[1] == kek.key_id.encode("ascii")


def _same_value(aad: bytes, wrap_aad: bytes) -> bool:
    """Whether the payload AAD names the value the wrap AAD names, in the matching kind."""
    fields = _wrap_fields(wrap_aad)
    if fields is None:
        return False
    return aad == b"|".join([_PAYLOAD_PREFIX_OF_WRAP[fields[0]], *fields[2:]])


def _bytes(value: Stored) -> bytes:
    """A driver may hand ``bytea`` back as ``memoryview``; anything else is refused."""
    if isinstance(value, bytes | memoryview):
        return bytes(value)
    raise TypeError("bytes are required")


def _decrypt(key: bytes, nonce: bytes, data: bytes, aad: bytes) -> bytes:
    if len(nonce) != NONCE_LENGTH:  # AES-GCM takes other lengths; ours are always 12 bytes
        raise DecryptFailed
    plaintext: bytes | None = None
    # Raised after the block, so DecryptFailed carries no __context__.
    with contextlib.suppress(InvalidTag, ValueError):
        plaintext = AESGCM(key).decrypt(nonce, data, aad)
    if plaintext is None:
        raise DecryptFailed
    return plaintext


def wrap(dek: bytes, kek: Kek, wrap_aad: bytes) -> tuple[bytes, bytes]:
    """``(wrapped_dek, dek_nonce)``: the DEK under ``kek``, a fresh random nonce. ``kek`` is a
    current KEK (``KeyRetired`` otherwise), and ``wrap_aad`` names its id."""
    _writing(kek)
    if not isinstance(dek, bytes) or len(dek) != KEY_LENGTH:
        raise ValueError("a data key is exactly 32 bytes")
    if not _names_kek(wrap_aad, kek):
        raise ValueError("the wrap AAD must name the id of the KEK that wraps")
    nonce = secrets.token_bytes(NONCE_LENGTH)
    return AESGCM(kek.key).encrypt(nonce, dek, wrap_aad), nonce


def unwrap(wrapped_dek: Stored, dek_nonce: Stored, kek: Kek | RetiredKek, wrap_aad: bytes) -> bytes:
    """The DEK, or ``DecryptFailed`` (also when ``wrap_aad`` names another KEK's id). A
    current or a retired KEK unwraps."""
    _opening(kek)
    if not _names_kek(wrap_aad, kek):
        raise DecryptFailed
    dek = _decrypt(kek.key, _bytes(dek_nonce), _bytes(wrapped_dek), wrap_aad)
    if len(dek) != KEY_LENGTH:
        raise DecryptFailed
    return dek


def seal(plaintext: bytes, aad: bytes, kek: Kek, wrap_aad: bytes) -> Sealed:
    """Encrypt ``plaintext`` under a new DEK and wrap the DEK under ``kek``, a current KEK
    (``KeyRetired`` otherwise). Both AADs name the same value, the wrap AAD ``kek``'s id."""
    _writing(kek)
    if not isinstance(plaintext, bytes):
        raise TypeError("the plaintext is bytes")
    if not _names_kek(wrap_aad, kek):
        raise ValueError("the wrap AAD must name the id of the KEK that wraps")
    if not _same_value(aad, wrap_aad):
        raise ValueError("the payload AAD and the wrap AAD must name the same value")
    dek = secrets.token_bytes(KEY_LENGTH)
    nonce = secrets.token_bytes(NONCE_LENGTH)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
    wrapped, dek_nonce = wrap(dek, kek, wrap_aad)
    return Sealed(ciphertext, nonce, wrapped, dek_nonce, kek.key_id)


def open_sealed(
    ciphertext: Stored,
    nonce: Stored,
    wrapped_dek: Stored,
    dek_nonce: Stored,
    kek: Kek | RetiredKek,
    aad: bytes,
    wrap_aad: bytes,
) -> bytes:
    """Unwrap the DEK and decrypt the payload, or ``DecryptFailed``. A current or a retired KEK
    opens; the AADs must name the same value, and the wrap AAD ``kek``'s id."""
    if not _same_value(aad, wrap_aad):
        raise DecryptFailed
    dek = unwrap(wrapped_dek, dek_nonce, kek, wrap_aad)
    return _decrypt(dek, _bytes(nonce), _bytes(ciphertext), aad)


def rewrap(
    wrapped_dek: Stored,
    dek_nonce: Stored,
    old: Kek | RetiredKek,
    old_wrap_aad: bytes,
    new: Kek,
    new_wrap_aad: bytes,
) -> tuple[bytes, bytes]:
    """Move a wrapped DEK from ``old`` (current or retired) to ``new`` (a current KEK;
    ``KeyRetired`` otherwise). The DEK, the payload and the value both wrap AADs name stay the
    same: only the KEK changes."""
    _writing(new)
    old_fields, new_fields = _wrap_fields(old_wrap_aad), _wrap_fields(new_wrap_aad)
    if (
        old_fields is None
        or new_fields is None
        or [old_fields[0], *old_fields[2:]] != [new_fields[0], *new_fields[2:]]
    ):
        raise ValueError("a rewrap keeps the value its wrap AAD names; only the KEK changes")
    return wrap(unwrap(wrapped_dek, dek_nonce, old, old_wrap_aad), new, new_wrap_aad)
