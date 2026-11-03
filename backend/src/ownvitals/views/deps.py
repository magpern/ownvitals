"""Shared pieces of the read routes: the clock (a dependency, so tests fix "now") and the stream
states of one user, settled from the facts ``views.reads`` collected."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends

from ownvitals.views.reads import StatusFacts
from ownvitals.views.states import StreamState, stream_state


async def view_now() -> datetime:
    return datetime.now(UTC)


Now = Annotated[datetime, Depends(view_now)]


def settle(facts: StatusFacts, stream: str, now: datetime) -> StreamState:
    return stream_state(facts.connection, facts.streams.get(stream), now)
