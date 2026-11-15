"""Alembic environment: async engine as the owner role, URL taken from ``Settings``.

``alembic upgrade head`` (compose's ``migrate``) is a process of its own, and this file is its
entry point: run from alembic's command line it installs the egress hook (kind ``migrate``: the
configured database hosts only) before the engine can connect (ADR 0009, #284). A caller that
runs alembic inside its own process (``alembic.command``, the tests) keeps that process's hook.
"""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection

from ownvitals.config import Settings
from ownvitals.db import create_migrations_engine, event_loop_factory
from ownvitals.egress import install_egress_hook, require_standard_event_loop

# Tables arrive with the milestones that need them; autogenerate gets a MetaData then.
target_metadata = None

settings = Settings()
if context.config.cmd_opts is not None:  # alembic's command line, not an in-process caller
    install_egress_hook("migrate", settings)


def run_migrations_offline() -> None:
    context.configure(
        url=settings.migrations_sqlalchemy_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    require_standard_event_loop()  # the only loop the hook sees (ADR 0009 item 6)
    engine = create_migrations_engine(settings, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations(), loop_factory=event_loop_factory())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
