"""Keeps web's copy of the webhook-secret HMACs fresh (PLAN section 4.4, ADR 0008; #322).

A background task of the web process (``start``, from ``ownvitals.web``'s lifespan). It re-reads
the entries with ``load_cache`` every quarter of the cache's TTL, so one or two failed reads in a
row still leave a usable copy. A read that fails or takes longer than one interval keeps the
previous copy; once that copy is older than the TTL the receiver answers 503 (fail closed) until
a read succeeds. Logs carry the error class only, never an entry, a pepper or a database URL.
"""

import asyncio
import contextlib
import logging
from datetime import timedelta

from fastapi import FastAPI

from ownvitals.withings.webhook_cache import Cache, load_cache

logger = logging.getLogger(__name__)

# Reads per TTL: a copy survives this many minus one failed reads in a row.
READS_PER_TTL = 4


def interval_for(ttl: timedelta) -> float:
    """Seconds between reads for a cache with ``ttl``."""
    return ttl.total_seconds() / READS_PER_TTL


async def refresh_once(app: FastAPI, limit: float) -> bool:
    """One read. ``False``, and the previous copy kept, when it fails or takes over ``limit`` s."""
    try:
        async with asyncio.timeout(limit):
            await load_cache(app)
    except Exception as exc:  # a background task must never die on one bad read
        logger.warning("webhook cache not refreshed", extra={"error_type": type(exc).__name__})
        return False
    return True


async def run(app: FastAPI, interval: float) -> None:
    """Reads forever, one interval apart, the first one interval after start (the lifespan has
    just read once)."""
    while True:
        await asyncio.sleep(interval)
        await refresh_once(app, interval)


def start(app: FastAPI, ttl: timedelta) -> asyncio.Task[None]:
    """Give ``app`` a ``Cache`` with ``ttl`` (kept by every later read) and start the task.
    Call it before the first ``load_cache`` so that read already carries the TTL."""
    cache = getattr(app.state, "webhook_cache", None)
    if not isinstance(cache, Cache):
        cache = Cache()
        app.state.webhook_cache = cache
    cache.ttl = ttl
    return asyncio.ensure_future(run(app, interval_for(ttl)))


async def stop(task: asyncio.Task[None] | None) -> None:
    """Cancel the task and wait for it to end."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
