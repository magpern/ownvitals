"""``/api/connections``: list, connect and disconnect (ADR 0004 section 7).

Connect and disconnect need recent authentication and a step-up (``require_sensitive``).
With no Withings flow yet, every provider but the test-only one answers ``unknown_provider``.
The test provider is registered only when ``environment`` is ``test``, so production has no
connect target. Bodies that fail validation are refused without echoing them.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr, ValidationError

from ownvitals.auth.deps import require_sensitive, require_user
from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.repository import (
    ConnectionInfo,
    connect,
    disconnect,
    list_connections,
)
from ownvitals.connections.tables import PROVIDER_NAME, TEST_PROVIDER, live_providers
from ownvitals.context import UserContext

router = APIRouter(prefix="/api/connections", tags=["connections"])

_STATUS = {
    "not_connected": 409,
    "decrypt_failed": 409,
    "needs_reconnect": 409,
    "key_unavailable": 503,
    "rate_limited": 429,
    "upstream_unavailable": 503,
    "revoke_failed": 502,
    "unknown": 502,
}


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


def _unknown() -> HTTPException:
    return HTTPException(status_code=404, detail={"reason": "unknown_provider"})


def _offered(request: Request, provider: str) -> bool:
    return PROVIDER_NAME.fullmatch(provider) is not None and provider in live_providers(
        request.app.state.settings.environment
    )


def _failure(failure: ConnectionFailure) -> JSONResponse:
    return JSONResponse(
        {"detail": {"reason": failure.category}},
        status_code=_STATUS.get(failure.category, 502),
    )


@router.get("")
async def get_connections(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> ConnectionsOut:
    rows = await list_connections(request.app.state.session_factory, ctx)
    return ConnectionsOut(connections=[ConnectionOut.of(row) for row in rows])


@router.post("/{provider}", response_model=ConnectionOut)
async def connect_provider(
    provider: str,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_sensitive)],
) -> ConnectionOut | JSONResponse:
    if not _offered(request, provider) or provider != TEST_PROVIDER:
        raise _unknown()
    invalid = False
    try:
        raw: Any = await request.json()
    except ValueError:
        invalid = True
        raw = None
    body: TestConnect | None = None
    if not invalid:
        try:
            body = TestConnect.model_validate(raw)
        except ValidationError:
            invalid = True
    if invalid or body is None:
        raise HTTPException(status_code=400, detail={"reason": "invalid_body"})
    try:
        info = await connect(
            request.app.state.session_factory,
            request.app.state.keyring,
            ctx,
            provider,
            body.access_token.get_secret_value(),
            body.refresh_token.get_secret_value(),
        )
    except ConnectionFailure as failure:
        return _failure(failure)
    return ConnectionOut.of(info)


@router.delete("/{provider}", status_code=204)
async def disconnect_provider(
    provider: str,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_sensitive)],
) -> None:
    if not _offered(request, provider):
        raise _unknown()
    await disconnect(request.app.state.session_factory, ctx, provider)
