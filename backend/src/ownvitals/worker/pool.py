"""The worker's database engine and its pool size (ADR 0005 sections 3 and 5; the loop is
``ownvitals.worker.loop``, which re-exports both)."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from ownvitals.config import Settings
from ownvitals.db import create_engine
from ownvitals.db.queue_check import install_queue_check


def worker_pool(settings: Settings) -> dict[str, Any]:
    """The worker engine's pool: per run one handler and one heartbeat connection, plus the
    ``LISTEN`` connection and one for claims, maintenance and the checks; an overflow of one per
    run covers a run that opens a second transaction while its first is open. With the cap of 16
    runs that is at most 50 connections."""
    return {
        "pool_size": 2 * settings.worker_concurrency + 2,
        "max_overflow": settings.worker_concurrency,
    }


def create_worker_engine(settings: Settings, **kwargs: Any) -> AsyncEngine:
    """The app role's engine with the queue check on every new connection (ADR 0005 section 3);
    the role check is ``create_engine``'s own."""
    engine = create_engine(settings, **{**worker_pool(settings), **kwargs})
    install_queue_check(engine)
    return engine
