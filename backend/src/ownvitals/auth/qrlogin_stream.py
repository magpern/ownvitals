"""The desktop's QR login stream: server-sent events of its request's state (ADR 0011 section 5).

``GET /api/auth/qr/{id}/events`` sends ``scanned`` (nothing about the phone), then one of
``approved``, ``denied``, ``expired`` or ``completed``, and closes; a comment line every 15 s keeps
proxies open. It carries the state only: never the nonce, a session token or anything about the
phone. The session cookie arrives on the desktop's following ``POST .../complete``.

**Bounded** (T89): one live stream per row (a new one closes the older), at most
``qr_streams_per_ip`` open streams per IP (IPv6 by /64) and ``qr_streams_per_process`` per web
process; over either, 429. A slot expires a little after the longest stream, so one that was
never released can't lock anyone out for good.

Wake-ups come from one ``LISTEN qr_logins`` connection per web process (a ``jobs.hub.EventHub``
on its own channel), which only sets the waiting streams' events. A stream holds no database
connection while it waits; it re-reads its row with one short query on a wake-up and every
``POLL_SECONDS``, so a lost notification only delays, and it ends at the row's deadline at the
latest. SWAG must not buffer this path (threat model O6): the response says
``X-Accel-Buffering: no`` and ``Cache-Control: no-store``.
"""

import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth import qrlogin
from ownvitals.db import system_session
from ownvitals.jobs.hub import EventHub, Subscription

KEEPALIVE_SECONDS: float = 15.0
POLL_SECONDS: float = 5.0
# A stream never outlives its row's last deadline; this is the backstop.
MAX_STREAM_SECONDS: float = qrlogin.LONGEST.total_seconds() + 5.0
KEEPALIVE_FRAME: Final = ": keepalive\n\n"
EVENTS: Final = frozenset({"scanned", "approved", "denied", "expired", "completed"})


def frame(state: str) -> str:
    """``id`` is the state too, so a reconnect after the end can be answered 204."""
    return f'id: {state}\nevent: {state}\ndata: {{"state":"{state}"}}\n\n'


@dataclass(eq=False)
class Slot:
    row_id: UUID
    ip_key: str
    expires: float
    superseded: bool = False
    subscription: Subscription | None = field(default=None)

    def close(self) -> None:
        """A newer stream for the same row took over: this one ends at its next wake-up."""
        self.superseded = True
        if self.subscription is not None:
            self.subscription.wake.set()


class StreamSlots:
    """The open QR streams of this web process."""

    def __init__(self, per_ip: int, per_process: int) -> None:
        self.per_ip = per_ip
        self.per_process = per_process
        self._slots: set[Slot] = set()
        self._by_row: dict[UUID, Slot] = {}

    def _prune(self, now: float) -> None:
        for slot in [s for s in self._slots if s.expires <= now]:
            self.release(slot)

    def acquire(self, row_id: UUID, ip_key: str) -> Slot | None:
        """A slot, or ``None`` when the IP or the process is at its limit. A stream already
        open for the row is closed (and its slot freed) first: it is the same desktop's."""
        now = time.monotonic()
        self._prune(now)
        older = self._by_row.get(row_id)
        others = [s for s in self._slots if s is not older]
        if len(others) >= self.per_process:
            return None
        if sum(1 for s in others if s.ip_key == ip_key) >= self.per_ip:
            return None
        if older is not None:
            older.close()
            self.release(older)
        slot = Slot(row_id, ip_key, now + MAX_STREAM_SECONDS + 2 * KEEPALIVE_SECONDS)
        self._slots.add(slot)
        self._by_row[row_id] = slot
        return slot

    def release(self, slot: Slot) -> None:
        self._slots.discard(slot)
        if self._by_row.get(slot.row_id) is slot:
            del self._by_row[slot.row_id]

    @property
    def open(self) -> int:
        return len(self._slots)


async def _read(
    factory: async_sessionmaker[AsyncSession], row_id: UUID, presession_id: UUID
) -> qrlogin.StreamState | None:
    async with system_session(factory) as db:
        return await qrlogin.stream_state(db, row_id, presession_id)


async def qr_stream(
    factory: async_sessionmaker[AsyncSession],
    hub: EventHub,
    slot: Slot,
    presession_id: UUID,
) -> AsyncIterator[str]:
    started = time.monotonic()
    last_sent = started
    sent: str | None = None
    with hub.subscribe(slot.row_id) as subscription:
        slot.subscription = subscription
        while not hub.closing and not slot.superseded:
            current = await _read(factory, slot.row_id, presession_id)
            state = "expired" if current is None else current.state
            if state != sent and state in EVENTS:
                yield frame(state)
                sent, last_sent = state, time.monotonic()
            if state in qrlogin.ENDED:
                return
            now = time.monotonic()
            if now - started >= MAX_STREAM_SECONDS:
                return
            left = current.seconds_left if current is not None else 0.0
            # Re-read on a wake-up, every POLL_SECONDS, and just after the state's deadline.
            await subscription.wait(min(POLL_SECONDS, left + 0.05))
            if time.monotonic() - last_sent >= KEEPALIVE_SECONDS:
                last_sent = time.monotonic()
                yield KEEPALIVE_FRAME


@contextlib.contextmanager
def held(slots: StreamSlots, slot: Slot):
    try:
        yield slot
    finally:
        slots.release(slot)
