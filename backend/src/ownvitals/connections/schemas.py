"""The bodies of ``/api/connections`` (``routes.py``): the test provider's connect body and the
answers. No answer carries a credential."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr

from ownvitals.connections.repository import ConnectionInfo
from ownvitals.withings.disconnect import REMOVAL


class TestConnect(BaseModel):
    access_token: SecretStr = Field(min_length=8, max_length=256)
    refresh_token: SecretStr = Field(min_length=8, max_length=256)


class ConnectionOut(BaseModel):
    id: UUID
    provider: str
    status: str
    last_error: str | None
    connected_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None

    @classmethod
    def of(cls, info: ConnectionInfo) -> "ConnectionOut":
        return cls(
            id=info.id,
            provider=info.provider,
            status=info.status,
            last_error=info.last_error,
            connected_at=info.connected_at,
            last_used_at=info.last_used_at,
            expires_at=info.expires_at,
        )


class ConnectionsOut(BaseModel):
    connections: list[ConnectionOut]


class DisconnectOut(BaseModel):
    """What a disconnect did. ``removal`` is set exactly when ``revoke_failed`` is."""

    status: str = "disconnected"
    revoke_failed: bool
    removal: str | None
    imported_deleted: bool

    @classmethod
    def of(cls, revoke_failed: bool, imported_deleted: bool) -> "DisconnectOut":
        return cls(
            revoke_failed=revoke_failed,
            removal=REMOVAL if revoke_failed else None,
            imported_deleted=imported_deleted,
        )
