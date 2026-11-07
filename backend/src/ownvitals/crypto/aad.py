"""Key kinds, key ids and the AAD strings of ``ownvitals.crypto.envelope`` (ADR 0004 sections 2
to 4). Pure strings: no key material, no cryptographic primitive, no database, no files.

``ownvitals.crypto.envelope`` re-exports every public name here; callers import them from the
envelope, which ``tests/test_crypto_guard.py`` limits to its allowed packages.

- AAD strings start with a version prefix per kind of value and join fields with ``|``; no field
  can contain one (UUIDs in canonical text form, provider names and key ids from their
  patterns), so they cannot collide.
- The ring holds three **kinds** of key (``KeyKind``): KEKs, and the webhook-secret seed and
  pepper of ADR 0004 section 4. Each kind has its own id prefix and its own check message.
"""

import enum
import re
from uuid import UUID

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


def is_key_id(value: object, kind: KeyKind = KeyKind.KEK) -> bool:
    """Whether ``value`` is an id of ``kind`` (for a KEK ``^kek-[a-z0-9-]{1,28}$``)."""
    return isinstance(value, str) and kind.id_pattern.fullmatch(value) is not None


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


def _app_id(value: int) -> str:
    """A ``withings_apps.id``: a positive 32-bit integer, so the AAD cannot contain ``|``."""
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise ValueError("the app id is a positive integer")
    return str(value)


def app_aad(app_id: int) -> bytes:
    """``ownvitals-app-v1|<app_id>``: a Withings app's client secret (ADR 0004 section 2)."""
    return f"ownvitals-app-v1|{_app_id(app_id)}".encode()


def app_wrap_aad(key_id: str, app_id: int) -> bytes:
    """``ownvitals-app-dek-v1|<key_id>|<app_id>``: the wrapped DEK of that client secret."""
    return f"ownvitals-app-dek-v1|{_key_id(key_id)}|{_app_id(app_id)}".encode()
