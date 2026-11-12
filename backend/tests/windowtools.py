"""Keep a count-to-the-limit test inside one rate-limit window.

``auth/ratelimit.py`` counts in fixed wall-clock windows (``floor(epoch / window) * window`` on the
Postgres clock). A test that makes ``limit + 1`` calls and expects the last to be refused is wrong
when the calls straddle a window boundary: the count restarts and the last call is allowed (#486).
``clear_of_window`` waits, at the start of such a test, until the next boundary is far enough away
for the whole test. It reads the clock the limiter reads, the database's ``clock_timestamp()``,
because the Postgres container's clock may differ from this process's. It changes no assertion and
touches no limiter internals. Synthetic tests only."""

import asyncio
import math
import os
from collections.abc import Awaitable, Callable

from sqlalchemy import create_engine, text

# How long the longest count test may run inside its window, and the safety margin on top of it.
NEEDED_SECONDS = 30.0
MARGIN_SECONDS = 2.0

EpochClock = Callable[[], Awaitable[float]]
Sleep = Callable[[float], Awaitable[object]]


async def database_epoch() -> float:
    """The test Postgres's ``clock_timestamp()`` as epoch seconds (a superuser probe)."""
    url = os.environ["OWNVITALS_TEST_DATABASE_URL"]

    def read() -> float:
        engine = create_engine(url, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as connection:
                return float(
                    connection.execute(
                        text("SELECT extract(epoch FROM clock_timestamp())::float8")
                    ).scalar_one()
                )
        finally:
            engine.dispose()

    return await asyncio.to_thread(read)


async def clear_of_window(
    window_seconds: int,
    *,
    needed: float = NEEDED_SECONDS,
    margin: float = MARGIN_SECONDS,
    clock: EpochClock = database_epoch,
    sleep: Sleep = asyncio.sleep,
) -> float:
    """Return once at least ``needed + margin`` seconds remain in the current window of
    ``window_seconds``, sleeping past the boundary when fewer do. Returns the seconds slept."""
    if window_seconds <= 2 * (needed + margin):
        raise ValueError(f"a {window_seconds} s window is too short for a {needed} s test")
    slept = 0.0
    while True:
        now = await clock()
        remaining = window_seconds - (now % window_seconds)
        if remaining > needed + margin:
            return slept
        pause = math.ceil(remaining) + margin  # just past the boundary
        await sleep(pause)
        slept += pause
