"""Transaction-owning sessions (ADR 0002 section 6).

``user_session`` is the only way to reach owned tables: it sets ``app.user_id`` for exactly one
transaction. ``system_session`` runs one transaction with no user: system tables only, and owned
tables return no rows there. Both own their transaction, so ``session.commit()`` and
``session.rollback()`` inside them raise (a new transaction would run without the setting). Only
the modules on the allowlist of ``tests/session_scan.py`` may open either. What this does and
does not do:

- Savepoints (``session.begin_nested()``) are allowed, and rolling one back keeps
  ``app.user_id`` (it is a transaction-level setting, not a savepoint-level one).
- Ending the transaction through any other path (``run_sync``, the connection's own
  ``rollback()``, a raw ``ROLLBACK``) is not intercepted. Rolling back **fails closed**: the
  setting is gone, so later statements on that session see no rows and cannot write owned rows.
- Committing through the connection (``(await session.connection()).commit()``) or a raw
  ``COMMIT`` would commit the partial work. That is forbidden by convention and the review gate,
  not enforced here.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, NoReturn

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.context import UserContext, require_context

SET_USER = text("SELECT set_config('app.user_id', :user_id, true)")


class TransactionOwnedError(RuntimeError):
    """``commit()`` or ``rollback()`` was called inside ``user_session``/``system_session``."""


def _refuse(name: str) -> Any:
    async def refuse(*_: object, **__: object) -> NoReturn:
        raise TransactionOwnedError(
            f"{name}() is not allowed here: the session owns the transaction "
            "(it commits on success and rolls back on an exception)"
        )

    return refuse


class _Guard:
    """Refuses transaction control by the caller; lets the context manager finish."""

    def __init__(self, session: AsyncSession) -> None:
        self.closing = False
        self._session = session
        self._commit, self._rollback = session.commit, session.rollback
        session.commit, session.rollback = _refuse("commit"), _refuse("rollback")  # type: ignore[method-assign]
        # Also catches a commit that bypasses the async facade, for example through run_sync.
        event.listen(session.sync_session, "before_commit", self._before_commit)

    def _before_commit(self, session: Any) -> None:
        # Leaving a savepoint also fires before_commit; that is not the end of the transaction.
        if not self.closing and not session.in_nested_transaction():
            raise TransactionOwnedError(
                "commit is not allowed here: the session owns the transaction"
            )

    async def finish(self, ok: bool) -> None:
        self.closing = True
        await (self._commit if ok else self._rollback)()


# The admin path's audited maint session (#12) owns its transaction the same way.
TransactionGuard = _Guard


@asynccontextmanager
async def _owned_session(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: str
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as session:
        await session.begin()
        guard = _Guard(session)
        try:
            # Transaction-local (third argument true): gone at commit or rollback, so a pooled
            # connection never carries it over. An empty id matches no owner.
            await session.execute(SET_USER, {"user_id": user_id})
            yield session
        except BaseException:
            await guard.finish(ok=False)
            raise
        else:
            await guard.finish(ok=True)


@asynccontextmanager
async def user_session(
    sessionmaker: async_sessionmaker[AsyncSession], ctx: UserContext
) -> AsyncIterator[AsyncSession]:
    """One transaction as ``ctx.user_id``: owned and system tables."""
    require_context(ctx)  # exactly a UserContext: no look-alike, subclass, UUID or dict
    async with _owned_session(sessionmaker, str(ctx.user_id)) as session:
        yield session


@asynccontextmanager
async def system_session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction with no user: system tables only; owned tables return no rows."""
    async with _owned_session(sessionmaker, "") as session:
        yield session
