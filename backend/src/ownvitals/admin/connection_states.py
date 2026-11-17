"""The admin area's view of Withings connections (issue 549; ADR 0002 sections 3 and 4).

``ownvitals_maint`` reads no column of ``connections`` (guard 11; ``contexttools`` checks it
reads no owner of any owned table). An account's connection state reaches the admin area only
through the two definers of ``docker/postgres/maint/70-admin-accounts.sql``, owned by
``ownvitals_admin_connections`` and executable by maint alone, in the admin transaction whose
``ok`` audit row they check:

- ``withings_states(db, ids)``: ``admin_withings_states``, in a ``user.list`` (or ``user.read``
  of that one account) transaction: each account's state, by id.
- ``withings_counts(db)``: ``admin_withings_counts``, in an ``overview.read`` transaction: how
  many accounts are in each state.

The three words are those of the user's own Connections page (``ownvitals.views.states``): no
live Withings connection is ``not_connected``, a broken one ``needs_reconnect``, and a connected
one the design's "Healthy". Nothing else comes back: no credential, Withings user id, app id,
error or time. A word the database does not admit here is a bug: ``UnexpectedState``.
"""

from collections.abc import Sequence
from typing import Final, Literal, get_args
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

WithingsState = Literal["healthy", "needs_reconnect", "not_connected"]
WITHINGS_STATES: Final[tuple[WithingsState, ...]] = get_args(WithingsState)
# The Users page plus one, as the definer admits.
MAX_IDS: Final = 101

_STATES = text("SELECT user_id, state FROM public.admin_withings_states(CAST(:ids AS uuid[]))")
_COUNTS = text("SELECT state, accounts FROM public.admin_withings_counts()")


class UnexpectedState(RuntimeError):
    """The definer returned a word outside ``WITHINGS_STATES``."""


def _word(value: object) -> WithingsState:
    for state in WITHINGS_STATES:
        if value == state:
            return state
    raise UnexpectedState("admin_withings_* returned an unknown state")


async def withings_states(db: AsyncSession, ids: Sequence[UUID]) -> dict[UUID, WithingsState]:
    """Each account's Withings state, for the accounts in ``ids`` that exist."""
    if not ids:
        return {}
    if len(ids) > MAX_IDS:
        raise ValueError("too many accounts for one read")
    rows = (await db.execute(_STATES, {"ids": list(ids)})).all()
    return {row.user_id: _word(row.state) for row in rows}


async def withings_counts(db: AsyncSession) -> dict[WithingsState, int]:
    """How many accounts are in each state; every state is present."""
    rows = (await db.execute(_COUNTS)).all()
    found = {_word(row.state): int(row.accounts) for row in rows}
    return {state: found.get(state, 0) for state in WITHINGS_STATES}
