"""The Withings connect's OAuth state (PLAN §4.3, ADR 0004 section 7; the flow is ``oauth.py``).

The state is kept as a SHA-256, bound to the session, for 10 minutes and one use, in the web
process only. A user who starts too often in a window gets no state.
"""

import asyncio
import hashlib
import secrets
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

STATE_TTL_SECONDS = 10 * 60
_STARTS = 30
_START_WINDOW = 15 * 60


@dataclass(slots=True)
class _Pending:
    session_id: UUID
    user_id: UUID
    app_id: int
    redirect_uri: str
    expires_at: float


class StateStore:
    """Hashed, single-use state for one web process."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._rows: dict[bytes, _Pending] = {}
        self._starts: dict[UUID, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def _purge(self, now: float) -> None:
        stale = [key for key, row in self._rows.items() if row.expires_at <= now]
        for key in stale:
            del self._rows[key]

    async def issue(
        self, *, session_id: UUID, user_id: UUID, app_id: int, redirect_uri: str
    ) -> str | None:
        """The state, or ``None`` when this user has started too often."""
        now = self._clock()
        async with self._lock:
            self._purge(now)
            recent = [stamp for stamp in self._starts[user_id] if now - stamp < _START_WINDOW]
            if len(recent) >= _STARTS:
                self._starts[user_id] = recent
                return None
            recent.append(now)
            self._starts[user_id] = recent
            state = secrets.token_urlsafe(32)
            self._rows[hashlib.sha256(state.encode()).digest()] = _Pending(
                session_id, user_id, app_id, redirect_uri, now + STATE_TTL_SECONDS
            )
        return state

    async def take(self, state: str) -> _Pending | None:
        """Consume the state. ``None`` when it is unknown, expired or already taken."""
        if not isinstance(state, str) or not state:
            return None
        now = self._clock()
        async with self._lock:
            self._purge(now)  # the only expiry check, so the pop below never sees a dead state
            return self._rows.pop(hashlib.sha256(state.encode()).digest(), None)
