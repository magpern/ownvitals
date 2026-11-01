"""Database engine, session factory and the readiness query. Nothing here connects or runs at
import time.

Sessions with a row-level-security user are in ``ownvitals.db.session``, the startup refusal of a
privileged role in ``ownvitals.db.role_check`` (ADR 0002 sections 5 and 6).
"""

import asyncio
import sys
from collections.abc import Callable
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ownvitals.config import Settings
from ownvitals.db.role_check import refuse_privileged_roles

CONNECT_TIMEOUT_SECONDS = 5


def discard_session_state(engine: AsyncEngine) -> None:
    """Run ``DISCARD ALL`` whenever a connection goes back to the pool.

    A plain rollback leaves ``WITH HOLD`` cursors, temp tables, prepared statements, advisory
    locks and session settings on the connection, and the next user of the pooled connection
    would inherit them (ADR 0002 section 6; a held cursor opened as user A can still be fetched
    from as user B). The
    engine is built with ``pool_reset_on_return=None`` so this handler is the whole reset: it
    rolls back first (``DISCARD ALL`` cannot run in a transaction block), then discards. If
    either step fails the pool invalidates the connection, so it is closed and never reused.

    Prepared statements: psycopg prepares a statement server side after it ran
    ``prepare_threshold`` times and remembers the names it prepared. ``DISCARD ALL`` drops them
    on the server, so psycopg's record must go too, or it would run a name the server no longer
    has ("prepared statement ... does not exist"). psycopg only looks at a command's status for
    that the first time it sees a query text, so the ``DISCARD ALL`` sent here is not reliably
    noticed. What reliably clears the record is ``Connection.rollback()`` while a transaction is
    open: it forgets the names and has the server deallocate them. That is the ``ROLLBACK`` that
    ``refuse_privileged_roles`` (``role_check``) runs after its query on every pool checkout.
    ``test_the_reset_keeps_one_connection_working_across_many_uses`` pins that on a pool of one
    connection: re-run it before changing the reset, the checkout check, the pool or the driver.
    """

    @event.listens_for(engine.sync_engine, "reset")
    def discard(dbapi_connection: Any, _record: object, reset_state: Any) -> None:
        if reset_state.terminate_only or not reset_state.asyncio_safe:
            return  # the connection is being closed, not reused
        dbapi_connection.rollback()
        dbapi_connection.autocommit = True
        try:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("DISCARD ALL")
            finally:
                cursor.close()
        finally:
            dbapi_connection.autocommit = False


def create_engine(settings: Settings, **kwargs: Any) -> AsyncEngine:
    """Build the app role's async engine (web and worker). It connects lazily, on first use.

    Every new connection is checked on its **login** role: a role that is a superuser, has
    BYPASSRLS or REPLICATION, is a member of any role, owns tables, a schema or the database, has
    other role settings or meets an ``app.*`` setting default is refused, and so is a login that
    runs as another role (a ``role`` default or connection option) or starts its sessions with
    ``app.user_id`` set (server configuration or connection options) (ADR 0002 section 5,
    ``role_check``).
    Every checkout checks the connection carries no ``app.user_id``, and every connection
    returned to the pool is wiped with ``DISCARD ALL``.
    """
    kwargs["pool_reset_on_return"] = None  # discard_session_state does the rollback itself
    engine = create_async_engine(
        settings.sqlalchemy_url(),
        pool_pre_ping=True,
        # Statement errors would otherwise carry the bound parameters (token hashes, emails)
        # into logs and tracebacks.
        hide_parameters=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
        **kwargs,
    )
    refuse_privileged_roles(engine)
    discard_session_state(engine)
    return engine


def create_migrations_engine(settings: Settings, **kwargs: Any) -> AsyncEngine:
    """The owner role's engine, for Alembic only. Never used by web or worker."""
    return create_async_engine(
        settings.migrations_sqlalchemy_url(),
        hide_parameters=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
        **kwargs,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def check_ready(factory: async_sessionmaker[AsyncSession]) -> None:
    """The readiness query: ``SELECT 1`` on a pooled connection. Raises when it fails."""
    async with factory() as session:
        await session.execute(text("SELECT 1"))


def event_loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """For ``asyncio.run(loop_factory=...)``: psycopg async needs a selector loop on Windows."""
    return asyncio.SelectorEventLoop if sys.platform == "win32" else None
