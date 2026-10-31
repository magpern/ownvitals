"""Two synthetic users at once, in every execution context of the harness (``ctx.py``: a web
request and a worker job; #15 adds scheduler, webhook and admin), on the test-only owned table as
the app role: nothing crosses (ADR 0002 sections 4 and 7, issue #5).

Both users run concurrently (asyncio, released together by a barrier), each adding rows and
reading them back in between, then try to read and delete each other's rows by id. ``verify``
asserts that nothing crossed: what each user saw, the rows as the superuser sees them, lookups
and deletes of the other user's ids, and a raw query as ``ownvitals_app`` without a user, which
sees nothing. ``test_the_check_detects_*`` run the same check against deliberately broken setups
and require it to fail. Nothing broken exists in production code.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text

from ctx import EXECUTION_CONTEXTS, ExecutionContext, Rows

USES = 4  # rows per user
TIMEOUT = 60  # seconds for the whole scenario: a user failing early must not hang the other
pytestmark = pytest.mark.usefixtures("owned_tables")


@dataclass
class User:
    label: str
    context: ExecutionContext
    user_id: UUID = field(default_factory=uuid4)
    ids: list[UUID] = field(default_factory=lambda: list[UUID]())
    seen: list[list[str]] = field(default_factory=lambda: list[list[str]]())

    def own(self, upto: int = USES) -> list[str]:
        return sorted(f"{self.label}-{i}" for i in range(upto))


async def flow(user: User, barrier: asyncio.Barrier) -> None:
    try:
        await barrier.wait()  # from here the two users' calls interleave
        for i in range(USES):
            user.ids.append(await user.context.add(user.user_id, f"{user.label}-{i}"))
            user.seen.append(await user.context.names(user.user_id))
    except BaseException:
        await barrier.abort()  # the other user stops waiting for a partner that failed first
        raise


async def run_scenario(alice: User, bob: User) -> None:
    barrier = asyncio.Barrier(2)
    async with asyncio.timeout(TIMEOUT):
        outcomes = await asyncio.gather(
            flow(alice, barrier), flow(bob, barrier), return_exceptions=True
        )
    failures = [o for o in outcomes if isinstance(o, BaseException)]
    if failures:  # the user that failed, not the one that found the barrier broken
        raise next(
            (f for f in failures if not isinstance(f, asyncio.BrokenBarrierError)), failures[0]
        )


def rows_as(url: str, sql: str, **params: object) -> list[Any]:
    """Rows on a plain connection of ``url``: the superuser sees what RLS hides; the app role
    without ``app.user_id`` sees nothing."""
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            result = connection.execute(text(sql), params)
            return list(result) if result.returns_rows else []
    finally:
        engine.dispose()


async def verify(alice: User, bob: User, database_url: str, app_database_url: str) -> None:
    """Raises ``AssertionError`` when anything crossed between the users."""
    for user, other in ((alice, bob), (bob, alice)):
        # 1. Every read during the run showed only the user's own rows, growing one by one.
        for i, seen in enumerate(user.seen):
            assert seen == user.own(i + 1), f"{user.label} was shown {seen}"
        final = await user.context.names(user.user_id)
        assert final == user.own(), f"{user.label} was shown {final}"
        # 2. The other user's ids answer like ids that do not exist; the own ones work.
        for row_id in other.ids:
            assert await user.context.find(user.user_id, row_id) is None, (
                f"{user.label} found {other.label}'s row"
            )
            assert not await user.context.delete(user.user_id, row_id), (
                f"{user.label} deleted {other.label}'s row"
            )
        assert await user.context.find(user.user_id, user.ids[0]) == f"{user.label}-0"
    # 3. The rows, as the superuser sees them: each owned by its user, none lost or moved.
    stored = {(r.owner_id, r.name) for r in rows_as(database_url, "SELECT * FROM t_parent")}
    expected = {(u.user_id, name) for u in (alice, bob) for name in u.own()}
    assert stored == expected, "a row is missing, extra or owned by the other user"
    # 4. The app role without a user (no user_session) sees no row at all.
    assert rows_as(app_database_url, "SELECT count(*) AS n FROM t_parent")[0].n == 0


PAIRS = {
    "web and web": ("web request", "web request"),
    "worker and worker": ("worker job", "worker job"),
    "web and worker": ("web request", "worker job"),
}


async def users(
    stack: AsyncExitStack, pair: tuple[str, str], app_url: str, **options: Any
) -> tuple[User, User]:
    """Alice and Bob, each in the execution context the pair names (one per user)."""
    alice_context = await stack.enter_async_context(EXECUTION_CONTEXTS[pair[0]](app_url, **options))
    bob_context = await stack.enter_async_context(EXECUTION_CONTEXTS[pair[1]](app_url, **options))
    return User("alice", alice_context), User("bob", bob_context)


@pytest.fixture
async def stack() -> AsyncIterator[AsyncExitStack]:
    async with AsyncExitStack() as exits:
        yield exits


@pytest.mark.parametrize("pair", PAIRS.values(), ids=PAIRS.keys())
async def test_two_users_at_once_never_cross(
    stack: AsyncExitStack, pair: tuple[str, str], database_url: str, app_database_url: str
) -> None:
    alice, bob = await users(stack, pair, app_database_url)
    await run_scenario(alice, bob)
    await verify(alice, bob, database_url, app_database_url)


@pytest.mark.parametrize("pair", PAIRS.values(), ids=PAIRS.keys())
async def test_rls_alone_keeps_the_users_apart(
    stack: AsyncExitStack, pair: tuple[str, str], database_url: str, app_database_url: str
) -> None:
    """The repository's owner filter removed: forced RLS is the second wall, and it holds."""
    alice, bob = await users(stack, pair, app_database_url, rows=Rows(filtered=False))
    await run_scenario(alice, bob)
    await verify(alice, bob, database_url, app_database_url)


async def test_a_user_failing_before_the_barrier_fails_the_run_cleanly(
    stack: AsyncExitStack, app_database_url: str
) -> None:
    alice, bob = await users(stack, PAIRS["web and worker"], app_database_url)

    class Broken:
        name = "broken"

        async def add(self, user: UUID, name: str) -> UUID:
            raise RuntimeError("alice failed")

    alice.context = Broken()  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(RuntimeError, match="alice failed"):
        await asyncio.wait_for(run_scenario(alice, bob), timeout=TIMEOUT / 2)


async def test_the_check_detects_a_row_that_crossed_users(
    stack: AsyncExitStack, database_url: str, app_database_url: str
) -> None:
    """One of Bob's rows is handed to Alice (as the superuser, standing in for an ownership
    bug): ``verify`` must fail."""
    alice, bob = await users(stack, PAIRS["web and worker"], app_database_url)
    await run_scenario(alice, bob)
    await verify(alice, bob, database_url, app_database_url)  # clean before the corruption
    rows_as(database_url, "UPDATE t_parent SET owner_id = :a WHERE id = :i",
            a=alice.user_id, i=bob.ids[-1])  # fmt: skip
    with pytest.raises(AssertionError, match="alice"):
        await verify(alice, bob, database_url, app_database_url)


@pytest.mark.parametrize("pair", PAIRS.values(), ids=PAIRS.keys())
async def test_the_check_detects_a_context_without_either_wall(
    stack: AsyncExitStack, pair: tuple[str, str], database_url: str, app_database_url: str
) -> None:
    """No owner filter and a session that bypasses RLS (the superuser's): the users see each
    other's rows, and ``verify`` must fail."""
    alice, bob = await users(
        stack, pair, app_database_url, rows=Rows(filtered=False), bypass_url=database_url
    )
    await run_scenario(alice, bob)
    with pytest.raises(AssertionError, match="was shown"):
        await verify(alice, bob, database_url, app_database_url)
