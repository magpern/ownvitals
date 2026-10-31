"""The repository filters on ``owner_id`` itself, with row-level security out of the picture
(ADR 0002 section 2: RLS is the second wall, not the only one).

Every other isolation test runs as the app role, where RLS hides the other user's row, so a
repository query that lost its ``owner_id = ctx.user_id`` would still pass. Here the execution
contexts run on a session factory of the *superuser* (``ctx.bypass_sessions``; it bypasses RLS
even on forced tables), with two users' rows in the table: only the repository's own filter
stands between the calls and the other user's row. The repositories of later issues (#13
connections, M1 health tables) use the same ``bypass_url`` option to show their filters hold.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from ctx import EXECUTION_CONTEXTS, ExecutionContext, Rows, bypass_sessions, make_context
from ownvitals.db import user_session

pytestmark = pytest.mark.usefixtures("owned_tables")
ALICE, BOB = uuid4(), uuid4()


async def count_all(superuser_url: str) -> int:
    """Rows in ``t_parent`` through the bypass factory, with a user set: RLS does not apply."""
    async with (
        bypass_sessions(superuser_url) as bypass,
        user_session(bypass, make_context(ALICE)) as db,
    ):
        return (await db.execute(text("SELECT count(*) FROM t_parent"))).scalar_one()


@pytest.fixture(params=sorted(EXECUTION_CONTEXTS))
async def bypassing(
    request: pytest.FixtureRequest, app_database_url: str, database_url: str
) -> AsyncIterator[ExecutionContext]:
    """An execution context whose sessions bypass RLS; the repository keeps its owner filter."""
    async with EXECUTION_CONTEXTS[request.param](app_database_url, bypass_url=database_url) as c:
        yield c


async def test_the_bypass_really_takes_rls_out_of_the_picture(
    bypassing: ExecutionContext, database_url: str, app_database_url: str
) -> None:
    await bypassing.add(ALICE, "alice-0")
    await bypassing.add(BOB, "bob-0")
    assert await count_all(database_url) == 2  # around RLS: both users' rows
    async with EXECUTION_CONTEXTS["worker job"](app_database_url, rows=Rows(False)) as plain:
        assert await plain.names(ALICE) == ["alice-0"]  # as the app role: RLS hides Bob's row


async def test_list_returns_only_the_callers_rows(bypassing: ExecutionContext) -> None:
    await bypassing.add(ALICE, "alice-0")
    await bypassing.add(BOB, "bob-0")
    assert await bypassing.names(ALICE) == ["alice-0"]
    assert await bypassing.names(BOB) == ["bob-0"]


async def test_find_and_delete_do_not_reach_another_users_row(
    bypassing: ExecutionContext,
) -> None:
    alices: UUID = await bypassing.add(ALICE, "alice-0")
    bobs: UUID = await bypassing.add(BOB, "bob-0")
    assert await bypassing.find(ALICE, bobs) is None
    assert not await bypassing.delete(ALICE, bobs)
    assert await bypassing.find(BOB, bobs) == "bob-0"  # still there
    assert await bypassing.find(ALICE, alices) == "alice-0"  # the filter is not just always false
    assert await bypassing.delete(ALICE, alices)
    assert await bypassing.names(ALICE) == []
    assert await bypassing.names(BOB) == ["bob-0"]


@pytest.mark.parametrize("name", sorted(EXECUTION_CONTEXTS))
async def test_without_the_filter_the_bypass_leaks(
    name: str, app_database_url: str, database_url: str
) -> None:
    """The proof the setup can fail: the same calls without the owner filter reach Bob's row."""
    async with EXECUTION_CONTEXTS[name](
        app_database_url, rows=Rows(filtered=False), bypass_url=database_url
    ) as broken:
        await broken.add(ALICE, "alice-0")
        bobs = await broken.add(BOB, "bob-0")
        assert await broken.names(ALICE) == ["alice-0", "bob-0"]
        assert await broken.find(ALICE, bobs) == "bob-0"
