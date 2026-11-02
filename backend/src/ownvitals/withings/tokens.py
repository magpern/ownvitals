"""Crash-safe Withings refresh (PLAN §4.3, ADR 0004 section 6).

The order is fixed: session lock, commit the intent, ``requesttoken`` with no retry budget,
then one transaction that compare-and-swaps ``generation + 1`` and clears the intent. The new
access token is returned only after that commit. An ambiguous outcome leaves the intent for
``recover_refresh``, which tries the stored refresh token once. An intent older than 8 hours,
or a failed recovery, sets ``needs_reconnect`` and the in-app notice (``last_error``). Nothing
here sends mail.

Two callers that race take the same lock. The one that finds the generation already moved
returns the stored token and does not call Withings.
"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.providers.withings import (
    OpenWithings,
    clear_intent,
    commit_refresh,
    give_up,
    hold_refresh_lock,
    mark_intent,
    open_withings,
)
from ownvitals.context import UserContext
from ownvitals.crypto.keks import KeyRing
from ownvitals.jobs.fence import Lease
from ownvitals.withings.client import WithingsClient
from ownvitals.withings.errors import TokenInvalid, WithingsError

Factory = async_sessionmaker[AsyncSession]
_OUTCOMES = frozenset(
    {"refreshed", "current", "recover", "needs_reconnect", "not_connected", "failed"}
)


@dataclass(frozen=True, slots=True, repr=False)
class RefreshResult:
    """What the caller may do next. ``access_token`` is set only after the new pair is stored,
    or when the stored pair is already current. ``repr`` never includes it."""

    outcome: str
    generation: int
    access_token: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in _OUTCOMES:
            raise ValueError("unknown refresh outcome")

    def __repr__(self) -> str:
        return f"RefreshResult(outcome={self.outcome!r}, generation={self.generation})"


def _current(view: OpenWithings) -> RefreshResult:
    return RefreshResult("current", view.generation, view.access_token)


async def _apply(
    factory: Factory,
    ring: KeyRing,
    ctx: UserContext,
    view: OpenWithings,
    client: WithingsClient,
    *,
    recovery: bool,
    lease: Lease | None,
    after_response: Callable[[], None] | None,
    after_commit: Callable[[], None] | None,
) -> RefreshResult:
    """One ``requesttoken``. Recovery that does not store the pair gives up."""
    invalid = False
    ambiguous = False
    other = False
    issued = None
    try:
        issued = await client.refresh(view.refresh_token)
    except TokenInvalid:
        invalid = True
    except WithingsError as exc:
        ambiguous = exc.ambiguous
        other = not ambiguous
    if after_response is not None and issued is not None:
        after_response()
    if issued is not None:
        saved = await commit_refresh(
            factory,
            ring,
            ctx,
            view.id,
            view.generation,
            issued.access_token.get_secret_value(),
            issued.refresh_token.get_secret_value(),
            int(issued.expires_in),
            lease,
        )
        if saved:
            if after_commit is not None:
                after_commit()
            return RefreshResult(
                "refreshed", view.generation + 1, issued.access_token.get_secret_value()
            )
        return RefreshResult("current", view.generation)
    if invalid or (recovery and (ambiguous or other)):
        await give_up(factory, ctx, view.id, view.generation, lease)
        return RefreshResult("needs_reconnect", view.generation)
    if ambiguous:
        return RefreshResult("recover", view.generation)
    await clear_intent(factory, ctx, view.id, view.generation, lease)
    return RefreshResult("failed", view.generation)


async def _resolve(
    factory: Factory,
    ring: KeyRing,
    ctx: UserContext,
    view: OpenWithings,
    client: WithingsClient,
    lease: Lease | None,
    after_response: Callable[[], None] | None,
    after_commit: Callable[[], None] | None,
) -> RefreshResult:
    if view.stale_intent:
        await give_up(factory, ctx, view.id, view.generation, lease)
        return RefreshResult("needs_reconnect", view.generation)
    return await _apply(
        factory,
        ring,
        ctx,
        view,
        client,
        recovery=True,
        lease=lease,
        after_response=after_response,
        after_commit=after_commit,
    )


async def refresh_tokens(
    factory: Factory,
    ring: KeyRing,
    ctx: UserContext,
    connection_id: UUID,
    client: WithingsClient,
    *,
    lease: Lease | None = None,
    after_response: Callable[[], None] | None = None,
    after_commit: Callable[[], None] | None = None,
) -> RefreshResult:
    """PLAN section 4.3 steps 1 to 3. ``client.refresh`` is never given a retry budget.

    ``after_response`` runs once the answer is in hand and before the compare-and-swap, so a
    crash there leaves the intent. ``after_commit`` runs after the new generation is durable
    and before the access token is returned.
    """
    try:
        before = await open_withings(factory, ring, ctx, connection_id)
    except ConnectionFailure as failure:
        if failure.category == "not_connected":
            return RefreshResult("not_connected", 0)
        raise
    async with hold_refresh_lock(factory, connection_id):
        try:
            view = await open_withings(factory, ring, ctx, connection_id)
        except ConnectionFailure as failure:
            if failure.category == "not_connected":
                return RefreshResult("not_connected", before.generation)
            raise
        if view.intent is not None:
            return await _resolve(
                factory, ring, ctx, view, client, lease, after_response, after_commit
            )
        if view.generation != before.generation:
            return _current(view)
        marked = await mark_intent(factory, ctx, connection_id, view.generation, lease)
        if marked == "cancelled":
            return RefreshResult("not_connected", view.generation)
        if marked != "marked":
            return _current(await open_withings(factory, ring, ctx, connection_id))
        return await _apply(
            factory,
            ring,
            ctx,
            view,
            client,
            recovery=False,
            lease=lease,
            after_response=after_response,
            after_commit=after_commit,
        )


async def recover_refresh(
    factory: Factory,
    ring: KeyRing,
    ctx: UserContext,
    connection_id: UUID,
    client: WithingsClient,
    *,
    lease: Lease | None = None,
) -> RefreshResult:
    """The one recovery try (PLAN §4.3 step 4). No intent means the new generation is in use."""
    async with hold_refresh_lock(factory, connection_id):
        try:
            view = await open_withings(factory, ring, ctx, connection_id)
        except ConnectionFailure as failure:
            if failure.category == "not_connected":
                return RefreshResult("not_connected", 0)
            raise
        if view.intent is None:
            return _current(view)
        return await _resolve(factory, ring, ctx, view, client, lease, None, None)
