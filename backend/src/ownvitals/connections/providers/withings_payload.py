"""What a Withings row on ``connections`` holds (ADR 0004 sections 6 and 7; the rows are written
by ``withings.py`` and ``withings_refresh.py``): the token checks, the sealed token payload and
the public part. Never logged."""

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.tables import connections
from ownvitals.crypto.envelope import canonical_json

PROVIDER = "withings"
_REFRESH_TTL = timedelta(days=365)


_TOKEN_MIN = 8
_TOKEN_MAX = 256
_C = connections.c


Factory = async_sessionmaker[AsyncSession]


def _token(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN_MIN <= len(value) <= _TOKEN_MAX:
        raise ConnectionFailure("unknown")
    return value


def _payload(access: str, refresh: str, access_expires_at: datetime) -> bytes:
    return canonical_json(
        {
            "access_expires_at": access_expires_at.isoformat(),
            "access_token": access,
            "refresh_token": refresh,
        }
    )


def _public(app_id: int, userid: int, scopes: list[str]) -> dict[str, Any]:
    return {"app_id": app_id, "withings_userid": userid, "scopes": scopes}


def _as_public(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ConnectionFailure("decrypt_failed")
    return value
