"""Alembic environment: async engine as the owner role, URL taken from ``Settings``."""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection

from ownvitals.config import Settings
from ownvitals.db import create_migrations_engine, event_loop_factory

# Tables arrive with the milestones that need them; autogenerate gets a MetaData then.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=Settings().migrations_sqlalchemy_url(),
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
    engine = create_migrations_engine(Settings(), poolclass=pool.NullPool)
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
