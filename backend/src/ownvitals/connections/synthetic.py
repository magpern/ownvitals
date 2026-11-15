"""The AI evaluation's credential-free connection row (ADR 0014 section 9; #531).

The evaluation seeder's synthetic accounts need a connection row for their measure groups and
device (both reference one), but they never reach a provider. ``create_synthetic_connection``
inserts the test provider's row as ``disconnected`` with no credential at all (every secret
column NULL, which the table's ``connections_credentials`` check admits only together), so
there is no token to seal, open or refresh, and no key ring is needed. It runs in the caller's
transaction as the context's user (the evaluation's job fence, #527) and refuses unless the
database's name ends in ``_ai_eval`` and the context's account is marked synthetic
(``ownvitals.auth.synthetic``).
"""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.synthetic import SyntheticRefused, account_is_synthetic, eval_database
from ownvitals.connections.tables import DISCONNECTED, TEST_PROVIDER, connections
from ownvitals.context import UserContext, require_context


async def create_synthetic_connection(db: AsyncSession, ctx: UserContext) -> UUID:
    """The id of the account's credential-free test connection, inserted on the first call.
    ``db`` is a transaction as ``ctx``'s user (forced RLS); every statement names the owner."""
    require_context(ctx)
    if not await eval_database(db) or not await account_is_synthetic(db, ctx.user_id):
        raise SyntheticRefused("only a synthetic account in an evaluation database")
    existing = (
        await db.execute(
            select(connections.c.id).where(
                connections.c.owner_id == ctx.user_id,
                connections.c.provider == TEST_PROVIDER,
                connections.c.secret.is_(None),
            )
        )
    ).scalar()
    if existing is not None:
        return existing
    row_id = uuid4()
    await db.execute(
        pg_insert(connections).values(
            id=row_id,
            owner_id=ctx.user_id,
            provider=TEST_PROVIDER,
            status=DISCONNECTED,
            public={},
        )
    )
    return row_id
