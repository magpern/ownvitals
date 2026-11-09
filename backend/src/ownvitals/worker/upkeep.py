"""The worker's owner-less upkeep steps (ADR 0005 section 3, ADR 0003 section 1).

``quietly`` runs one maintenance step that returns a count: a failure is logged by its class
and retried at the next interval, a count above zero is logged, and ``DatabaseRoleError`` (a
privileged role or a drifted queue) is raised so that it stops the worker. ``purge`` is the
hourly purge tick. Logs carry counts and error classes only, never a row.
"""

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.auth.purge import purge_expired
from ownvitals.config import Settings
from ownvitals.db import DatabaseRoleError

logger = logging.getLogger("ownvitals.worker.loop")


async def quietly(name: str, step: Callable[[], Awaitable[int]]) -> None:
    try:
        count = await step()
    except DatabaseRoleError:
        raise
    except Exception as exc:
        logger.warning(
            "job maintenance failed", extra={"step": name, "error_type": type(exc).__name__}
        )
        return
    if count:
        logger.info("job maintenance", extra={"step": name, "count": count})


async def purge(factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    """The auth purge, once (ADR 0003 section 1). A failure is logged by its class and
    retried at the next interval; counts per table are logged, never a row."""
    try:
        counts = await purge_expired(factory, settings)
    except DatabaseRoleError:
        raise
    except Exception as exc:
        logger.warning("auth purge failed", extra={"error_type": type(exc).__name__})
        return
    logger.info("auth purge", extra={"deleted": counts})
