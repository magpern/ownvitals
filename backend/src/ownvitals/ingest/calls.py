"""One rate-limited Withings call. The token is spent before the call and never refunded.

The slot is released afterwards, including when the answer is only a capability. A lost lease
raises from the fence; the token still stays consumed. Each granted call counts once towards
the API quota metric, and a 601 answer once more towards the 601 count (PLAN section 4.4).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
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


async def _take(job: JobRun, account: Account, lane: int) -> rate.Claim:
    async with job.fenced() as fs:
        claim = await fs.db.run_sync(
            lambda sync: rate.claim(sync.connection(), account.app_id, account.connection_id, lane)
        )
    if not claim.granted:
        raise JobFailure("rate_limited", retry_after=_wait(claim.retry_at))
    return claim


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
