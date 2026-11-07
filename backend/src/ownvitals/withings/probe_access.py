"""The probe's (``probe.py``) access to Withings: the app's rate limiter on the user lane and
the connection's stored access token, refreshed once through ``tokens.py`` after a refusal."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from ownvitals.withings import rate
from ownvitals.withings.probe_call import Stopped

MAX_RATE_WAIT: Final = 120.0  # seconds the probe waits for the limiter in all, then stops


class _Bucket:
    """The app's rate limiter (``rate.py``) on the user lane: a token before every call."""

    def __init__(
        self,
        engine: AsyncEngine,
        app_id: int,
        connection_id: UUID,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_wait: float = MAX_RATE_WAIT,
    ) -> None:
        self._engine = engine
        self._app = app_id
        self._connection = connection_id
        self._sleep = sleep
        self._left = max_wait

    async def _claim(self) -> rate.Claim:
        while True:
            async with self._engine.begin() as conn:
                claim = await conn.run_sync(
                    lambda sync: rate.claim(sync, self._app, self._connection, rate.LANE_SYNC_NOW)
                )
            if claim.granted:
                return claim
            wait = 5.0
            if claim.retry_at is not None:
                wait = (claim.retry_at - datetime.now(UTC)).total_seconds()
            wait = min(max(wait, 1.0), 60.0)
            if wait > self._left:
                raise Stopped("rate_limited")
            self._left -= wait
            await self._sleep(wait)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        claim = await self._claim()
        try:
            yield
        finally:
            if claim.lease_id is not None:
                lease = claim.lease_id
                async with self._engine.begin() as conn:
                    await conn.run_sync(lambda sync: rate.release(sync, lease))

    async def pause(self, wait_seconds: float) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(lambda sync: rate.pause(sync, self._app, wait_seconds))


class _StoredTokens:
    """The connection's access token; one refresh through ``tokens.py`` after a refusal."""

    def __init__(self, access: str, renew: Callable[[], Awaitable[tuple[str, str]]]) -> None:
        self.access = access
        self.state = "stored"
        self._renew = renew

    async def renew(self) -> bool:
        outcome, access = await self._renew()
        self.state = outcome
        if access:
            self.access = access
            self.state = "refreshed" if outcome == "refreshed" else "current"
            return True
        return False
