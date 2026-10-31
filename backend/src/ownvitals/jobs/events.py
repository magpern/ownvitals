"""Job event payloads (ADR 0005 section 7): one Pydantic model per event kind, and the wake-up.

Every event a writer inserts goes through ``validated_payload``, and every event the SSE stream
sends goes through it again, so a row that holds anything else (a field the model does not name,
a long text, a nested value) is never shown. Each model has ``extra="forbid"`` and holds only enum
values, counts and timestamps of the job's own progress. **Never** in a payload: tokens or
credentials, error, exception or provider text, measurement values, ECG samples or finding
content.

``notify_event`` runs ``pg_notify('job_events', <job id>)`` in the transaction that inserts an
event: only the id, delivered at commit (a rolled-back event wakes nobody). The web processes
listen (``ownvitals.jobs.hub``) and read in a user session, so a notification about someone
else's job reveals nothing. ``docker/postgres/queue/20-queue.sql`` does the same for the events
the queue functions insert (a trigger would break the queue check, ADR 0005 section 3).
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Final
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.jobs.errors import CATEGORIES

CHANNEL: Final = "job_events"
NOTIFY = text("SELECT pg_notify('job_events', :job_id)")
# A progress count: whole, not negative, bounded (a page or window count, never a value).
Count = Annotated[int, Field(ge=0, le=1_000_000_000)]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Empty(Payload):
    """``queued``, ``cancel_requested``, ``cancelled``, ``completed``, ``requeued``."""


class Claimed(Payload):
    attempt: Annotated[int, Field(ge=1, le=10)]


class Progress(Payload):
    done: Count
    total: Count

    @model_validator(mode="after")
    def _done_within_total(self) -> "Progress":
        if self.done > self.total:
            raise ValueError("done must not exceed total")
        return self


class _Category(Payload):
    category: str

    @field_validator("category")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError("unknown failure category")
        return value


class RetryScheduled(_Category):
    available_at: datetime


class Failed(_Category):
    pass


# ADR 0005 section 7; the same kinds as the ``job_events.kind`` check of migration 0003.
MODELS: Final[Mapping[str, type[Payload]]] = {
    "queued": Empty,
    "claimed": Claimed,
    "progress": Progress,
    "retry_scheduled": RetryScheduled,
    "cancel_requested": Empty,
    "cancelled": Empty,
    "completed": Empty,
    "failed": Failed,
    "requeued": Empty,
}
# The stream ends after one of these (the job is over).
TERMINAL_KINDS: Final = frozenset({"completed", "failed", "cancelled"})


def validated_payload(kind: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """The payload as it is stored and shown: validated against the kind's model, JSON-ready
    (timestamps as text). ``ValueError`` (without the payload, which may be what is wrong with
    it) for an unknown kind or a payload the model refuses."""
    model = MODELS.get(kind)
    if model is None:
        raise ValueError("unknown event kind")
    values: Any = {} if payload is None else payload
    if not isinstance(values, Mapping):  # a stored scalar or list is no payload
        raise ValueError(f"the payload of a {kind!r} event does not match its model")
    try:
        return model.model_validate(dict(values)).model_dump(mode="json")
    except ValidationError:
        raise ValueError(f"the payload of a {kind!r} event does not match its model") from None


async def notify_event(db: AsyncSession, job_id: UUID) -> None:
    """Wake the web processes' streams for ``job_id`` when the transaction commits."""
    await db.execute(NOTIFY, {"job_id": str(job_id)})
