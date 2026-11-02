"""Remove stored credentials and keep the rows (ADR 0004 section 3, ``keys wipe``).

One transaction per user, with ``app.user_id`` set: credential columns become empty,
``needs_reconnect`` on one row per provider, then the account reset that ``admin mfa-reset``
does. The mail is the caller's, after the commit. ``VACUUM`` of this table runs outside any
transaction, as the owner role.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from ownvitals.auth.operator import WipeResult, wipe_user
from ownvitals.connections.repository import clear_credentials
from ownvitals.context import UserContext
from ownvitals.db import user_session

Factory = async_sessionmaker[AsyncSession]


async def wipe_owner(factory: Factory, ctx: UserContext) -> WipeResult | None:
    """Wipe one user. ``None`` when the user has no connection row and no authenticator."""
    async with user_session(factory, ctx) as db:
        providers = await clear_credentials(db, ctx)
        return await wipe_user(db, ctx.user_id, providers)


async def vacuum_connections(connection: AsyncConnection) -> None:
    """Rewrite the table so removed credential bytes are not left in dead tuples."""
    await connection.execute(text("VACUUM (FULL) public.connections"))
