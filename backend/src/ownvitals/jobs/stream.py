"""The SSE stream of a job's events (ADR 0005 section 7).

``event_stream`` yields the frames of one connection: ``id: <seq>``, ``event: <kind>``,
``data: <payload JSON>``. It replays events with a larger ``seq`` than ``after`` and then waits for
a wake-up from the hub (or, with the listener down, polls every ``POLL_SECONDS``). Every read is
a short user session (``ownvitals.jobs.progress``). A comment line every ``KEEPALIVE_SECONDS``
keeps proxies open. The stream ends after a terminal event, when
the job is gone, when the session is no longer valid for the same user (checked with each
keepalive interval), when the web process shuts down (``hub.closing``) and after
``MAX_STREAM_SECONDS``; the browser reconnects with its last id.
A payload is validated against its kind's model on the way out and sent empty if the stored row
does not match it, so nothing a model does not name ever reaches the browser.
"""

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.context import UserContext
from ownvitals.jobs.events import TERMINAL_KINDS, validated_payload
from ownvitals.jobs.hub import EventHub
from ownvitals.jobs.progress import StoredEvent, read_page

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS: float = 15.0
POLL_SECONDS: float = 5.0
MAX_STREAM_SECONDS: float = 600.0
PAGE_SIZE: Final = 200
TERMINAL_STATES: Final = frozenset({"completed", "failed", "cancelled"})
KEEPALIVE_FRAME: Final = ": keepalive\n\n"


def frame(event: StoredEvent) -> str:
    try:
        payload = validated_payload(event.kind, event.payload)
    except ValueError:
        logger.warning("job event does not match its model", extra={"kind": event.kind})
        payload = {}
    data = json.dumps(payload, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {data}\n\n"


async def event_stream(
    factory: async_sessionmaker[AsyncSession],
    hub: EventHub,
    ctx: UserContext,
    job_id: UUID,
    after: int,
    *,
    still_signed_in: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    started = time.monotonic()
    last_sent = started
    last_check = started
    with hub.subscribe(job_id) as subscription:
        while not hub.closing:
            page = await read_page(factory, ctx, job_id, after, PAGE_SIZE)
            if page is None:
                return
            for event in page.events:
                yield frame(event)
                after = event.seq
                last_sent = time.monotonic()
                if event.kind in TERMINAL_KINDS:
                    return
            if len(page.events) == PAGE_SIZE:
                continue  # more to replay
            if not page.events and page.state in TERMINAL_STATES:
                return  # a reconnect after the end
            now = time.monotonic()
            if now - started >= MAX_STREAM_SECONDS:
                return
            if now - last_check >= KEEPALIVE_SECONDS:
                last_check = now
                if not await still_signed_in():
                    return
            interval = KEEPALIVE_SECONDS if hub.listening else POLL_SECONDS
            await subscription.wait(min(interval, max(MAX_STREAM_SECONDS - (now - started), 0.0)))
            if hub.closing:
                return
            if time.monotonic() - last_sent >= KEEPALIVE_SECONDS:
                last_sent = time.monotonic()
                yield KEEPALIVE_FRAME
