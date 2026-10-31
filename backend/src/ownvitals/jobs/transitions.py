"""The worker's state transitions (ADR 0005 sections 2, 4 and 6), each inside a fenced transaction.

Every transition out of ``running`` is one ``UPDATE ... WHERE id AND owner_id AND lease_version
AND state = 'running'`` that also takes the next ``seq`` and inserts its event; the fence has
locked the row already, so 0 rows only happens if the lease is gone (``LeaseLost``). A requeue of
a job with a cancel requested ends it ``cancelled`` instead (cancel-on-requeue). Every requeue
raises ``lease_version``, so nothing the old holder still tries can pass the fence. Terminal
states never change again: every statement here requires ``state = 'running'``.

The user's transitions (create, cancel) are in ``ownvitals.jobs.user``.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from ownvitals.jobs.errors import RETRYABLE, LeaseLost
from ownvitals.jobs.fence import FencedSession, insert_event

# Each statement spells the fence out: WHERE id, owner_id, lease_version and state = 'running'.
_TERMINAL = text(
    "UPDATE jobs SET state = :state, error_category = :category, finished_at = now(), "
    "next_seq = next_seq + 1, updated_at = now(), "
    "leased_by = NULL, lease_expires_at = NULL, heartbeat_at = NULL "
    "WHERE id = :job_id AND owner_id = :owner_id AND lease_version = :v AND state = 'running' "
    "RETURNING next_seq"
)
_REQUEUE = text(
    "UPDATE jobs SET state = 'queued', attempt = attempt - :refund, "
    "available_at = now() + make_interval(secs => :delay), lease_version = lease_version + 1, "
    "next_seq = next_seq + 1, updated_at = now(), "
    "leased_by = NULL, lease_expires_at = NULL, heartbeat_at = NULL "
    "WHERE id = :job_id AND owner_id = :owner_id AND lease_version = :v AND state = 'running' "
    "RETURNING next_seq, available_at"
)
_ATTEMPTS = text(
    "SELECT attempt, max_attempts FROM jobs "
    "WHERE id = :job_id AND owner_id = :owner_id AND lease_version = :v AND state = 'running'"
)
_NOTIFY_QUEUED = text("SELECT pg_notify('jobs_queued', '')")


@dataclass(frozen=True, slots=True)
class Attempts:
    attempt: int
    max_attempts: int


async def attempts(fs: FencedSession) -> Attempts:
    row = (await fs.db.execute(_ATTEMPTS, fs.lease.params())).first()
    if row is None:
        raise LeaseLost
    return Attempts(row.attempt, row.max_attempts)


async def _terminal(
    fs: FencedSession, state: str, category: str | None, kind: str, payload: dict[str, Any]
) -> str:
    seq = (
        await fs.db.execute(_TERMINAL, {**fs.lease.params(), "state": state, "category": category})
    ).scalar_one_or_none()
    if seq is None:
        raise LeaseLost
    await insert_event(fs.db, fs.lease, seq, kind, payload)
    return state


async def complete(fs: FencedSession) -> str:
    return await _terminal(fs, "completed", None, "completed", {})


async def fail(fs: FencedSession, category: str) -> str:
    return await _terminal(fs, "failed", category, "failed", {"category": category})


async def cancel(fs: FencedSession) -> str:
    return await _terminal(fs, "cancelled", None, "cancelled", {})


async def _requeue(
    fs: FencedSession, *, refund: bool, delay: float, kind: str, category: str | None
) -> str:
    row = (
        await fs.db.execute(
            _REQUEUE, {**fs.lease.params(), "refund": 1 if refund else 0, "delay": delay}
        )
    ).first()
    if row is None:
        raise LeaseLost
    payload: dict[str, Any] = {}
    if category is not None:
        payload = {"category": category, "available_at": row.available_at}
    await insert_event(fs.db, fs.lease, row.next_seq, kind, payload)
    if delay <= 0:
        await fs.db.execute(_NOTIFY_QUEUED)
    return "queued"


async def retry(fs: FencedSession, category: str, delay: float) -> str:
    """A failure: a new attempt after ``delay`` seconds when the category is retryable and
    attempts remain, else ``failed`` with the category; ``cancelled`` if a cancel was requested
    (ADR 0005 section 6)."""
    if fs.cancel_requested:
        return await cancel(fs)
    if category not in RETRYABLE:
        return await fail(fs, category)
    counts = await attempts(fs)
    if counts.attempt >= counts.max_attempts:
        return await fail(fs, category)
    return await _requeue(fs, refund=False, delay=delay, kind="retry_scheduled", category=category)


async def hand_back(fs: FencedSession) -> str:
    """Shutdown (ADR 0005 section 5): back to ``queued`` at once, the attempt not counted
    (``attempt - 1``, ``lease_version + 1``, event ``requeued``); ``cancelled`` if a cancel was
    requested."""
    if fs.cancel_requested:
        return await cancel(fs)
    return await _requeue(fs, refund=True, delay=0, kind="requeued", category=None)
