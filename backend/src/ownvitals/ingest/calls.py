"""One rate-limited Withings call. The token is spent before the call and never refunded.

A claim the limiter refuses for its turn, a slot or a token (``wait``, ``busy``, ``empty``) is
asked again within the attempt, at the limiter's ``retry_at``, for at most
``CLAIM_WAIT_SECONDS`` in all (#349): the parallel streams of one account and other accounts
compete for the same four slots, and a refusal there is no failure of the job. A refused claim
spends no token, so asking again never exceeds the limiter's rate. A 601 hold (``paused``), or a
wait that would pass the bound, still fails the attempt as ``rate_limited``. A cancel or a
shutdown seen while waiting stops the run at once.

The slot is released afterwards, including when the answer is only a capability. A lost lease
raises from the fence; the token still stays consumed. Each granted call counts once towards
the API quota metric, and a 601 answer once more towards the 601 count (PLAN section 4.4).
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from ownvitals.jobs import JobFailure
from ownvitals.jobs.run import JobRun
from ownvitals.withings import rate
from ownvitals.withings.capabilities import from_permanent, provider_plan_blocked
from ownvitals.withings.errors import (
    RATE_LIMITED_STATUS,
    Permanent,
    RateLimited,
    TokenInvalid,
    Transient,
)
from ownvitals.withings.metrics import record_api_call

_CALL_SECONDS = 30.0
CLAIM_WAIT_SECONDS: Final = 60.0  # waited in all for one call's claim, within the attempt
_ASK_AGAIN: Final = frozenset({"wait", "busy", "empty"})  # refusals that are only a queue
_STEP_FLOOR: Final = 0.2  # the limiter's own retry_at for a turn or a slot
_STEP_CAP: Final = 5.0  # well inside the 30 s the limiter remembers that an account asked
_JITTER_MAX: Final = 1.25  # each wait is stretched by up to this factor, never shortened


@dataclass(frozen=True, slots=True, repr=False)
class Account:
    """One connection's grant. ``repr`` has no access token."""

    connection_id: UUID
    app_id: int
    scopes: tuple[str, ...]
    zone: str
    access_token: str

    def __repr__(self) -> str:
        return f"Account(connection_id={self.connection_id}, app_id={self.app_id})"


@dataclass(frozen=True, slots=True)
class Classified:
    capability: str


@dataclass(frozen=True, slots=True)
class Got:
    skipped: int = 0
    zone: str | None = None
    lastupdate: int | None = None
    empty: bool = True
    no_devices: bool | None = None
    goals: dict[str, Any] | None = None


@dataclass
class Halt:
    """Stop after this many windows have been committed, as a lost lease would."""

    after_windows: int
    seen: int = 0


def _wait(moment: datetime | None) -> float:
    if moment is None:
        return 60.0
    return max(1.0, (moment - datetime.now(UTC)).total_seconds())


def _step(moment: datetime | None, asked: int = 0, factor: float = 1.0) -> float:
    """How long to wait before asking again, kept in bounds.

    The limiter's ``retry_at`` is the least; the floor doubles with each refusal already seen
    (``asked``), so a long wait makes fewer claims than the 5 a second of a fixed floor. ``factor``
    (1 to ``_JITTER_MAX``) spreads streams that were refused together.
    """
    if moment is None:
        return _STEP_CAP
    due = (moment - datetime.now(UTC)).total_seconds()
    return min(max(due, _STEP_FLOOR * 2**asked) * factor, _STEP_CAP)


async def _take(
    job: JobRun,
    account: Account,
    lane: int,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    budget: float = CLAIM_WAIT_SECONDS,
    jitter: Callable[[], float] = lambda: random.uniform(1.0, _JITTER_MAX),  # noqa: S311
) -> rate.Claim:
    """One granted claim, asked again while the refusal is only a queue and the bound allows."""
    left, asked = budget, 0
    while True:
        async with job.fenced() as fs:
            claim = await fs.db.run_sync(
                lambda sync: rate.claim(
                    sync.connection(), account.app_id, account.connection_id, lane
                )
            )
        if claim.granted:
            return claim
        step = _step(claim.retry_at, min(asked, 8), jitter())
        if claim.reason not in _ASK_AGAIN or step > left:
            raise JobFailure("rate_limited", retry_after=_wait(claim.retry_at))
        if job.cancel_requested or job.shutdown.is_set():
            await job.checkpoint()  # raises RunStopped: cancelled, shutdown or lost
        left -= step
        asked += 1
        await sleep(step)


async def _release(job: JobRun, claim: rate.Claim) -> None:
    if claim.lease_id is None:
        return
    lease = claim.lease_id
    async with job.fenced() as fs:
        await fs.db.run_sync(lambda sync: rate.release(sync.connection(), lease))


async def limited(job: JobRun, account: Account, lane: int, call: Any) -> Any:
    """Spend one token, make the call, then free the slot."""
    claim = await _take(job, account, lane)
    status: int | None = None
    try:
        async with job.provider_call(_CALL_SECONDS):
            return await call()
    except RateLimited as exc:
        status, wait = RATE_LIMITED_STATUS, exc.wait_seconds
        async with job.fenced() as fs:
            await fs.db.run_sync(lambda sync: rate.pause(sync.connection(), account.app_id, wait))
        raise JobFailure("rate_limited", retry_after=wait) from None
    except TokenInvalid:
        raise JobFailure("needs_reconnect") from None
    except Transient:
        raise JobFailure("upstream_unavailable") from None
    except Permanent as exc:
        if provider_plan_blocked(exc.api_status):
            raise JobFailure("not_connected") from None
        found = from_permanent(exc)
        if found is None:
            raise JobFailure("invalid_input") from None
        return Classified(found)
    finally:
        record_api_call(status=status)
        await _release(job, claim)
