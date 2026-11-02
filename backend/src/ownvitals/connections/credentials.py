"""What a caller receives after a row is decrypted: never a dict of plaintext fields.

``repr`` and ``str`` show nothing, and the object cannot be pickled. The decrypted values
exist only as attributes for the caller that just opened the row, and are dropped with it.
"""

from dataclasses import dataclass
from typing import Any, NoReturn
from uuid import UUID

from ownvitals.connections.tables import TEST_PROVIDER

# Secret fields of each provider's canonical JSON payload. Withings joins in M1.
PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    TEST_PROVIDER: frozenset({"access_token", "refresh_token"}),
}


@dataclass(frozen=True, slots=True, repr=False)
class Credentials:
    """One provider's decrypted tokens. Never logged, stored or serialized."""

    provider: str
    access_token: str
    refresh_token: str

    def __repr__(self) -> str:
        return "Credentials(...)"

    def __str__(self) -> str:
        return "Credentials(...)"

    def __reduce__(self) -> NoReturn:
        raise TypeError("credentials are never serialized")


@dataclass(frozen=True, slots=True, repr=False)
class OpenConnection:
    """A decrypted row for one call, plus the generation that was read."""

    id: UUID
    provider: str
    credentials: Credentials
    generation: int

    def __repr__(self) -> str:
        return f"OpenConnection(id={self.id}, provider={self.provider!r})"

    def __str__(self) -> str:
        return f"OpenConnection(id={self.id}, provider={self.provider!r})"

    def __reduce__(self) -> NoReturn:
        raise TypeError("an open connection is never serialized")


def build_credentials(provider: str, payload: dict[str, Any]) -> Credentials:
    """The provider's tokens from a decrypted payload. The error names no value."""
    expected = PAYLOAD_FIELDS.get(provider)
    if expected is None or set(payload) != expected:
        raise ValueError("the payload does not have the provider's fields")
    access = payload["access_token"]
    refresh = payload["refresh_token"]
    if not isinstance(access, str) or not isinstance(refresh, str):
        raise ValueError("the payload does not have the provider's fields")
    return Credentials(provider=provider, access_token=access, refresh_token=refresh)
