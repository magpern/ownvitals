"""The Withings app rate limiter (PLAN §4.4, ADR 0005 §8).

One Postgres token bucket per Withings app, budget 100 a minute of the documented 120. A
token is consumed when ``claim`` grants it and is never refunded: the call may already have
left. Four global slots lease the in-flight calls; ``release`` frees the slot only, and a
slot whose lease has expired is free the next time someone claims. Lanes, highest first, are
the same numbers as the job queue: user sync now, webhook, poll, reconcile, backfill. A
highest lane that is still waiting wins: one that asked within 10 seconds and has not been
granted since, or the caller itself (#370), so a lane whose job stopped asking holds nothing.
The same lane is round-robin, and one account's backfill is capped while another account's
backfill is waiting.

``pause`` is the 601 hold. The wait is finite: non-positive and NaN become 60 seconds, and
anything longer than 15 minutes, including an infinite provider value, becomes 15 minutes.
The authorization-code exchange (roadmap Amendment 1) uses ``claim_code_exchange`` and spends
the same budget as every other call.

The tables are system tables. The app role has no privilege on them; these functions are the
only path. Callers commit the claim before the HTTP call, so a crash after that commit keeps
the token and frees the slot only when the lease expires.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ownvitals.withings.errors import clamp_rate_wait

# Highest first. The same numbers as ``ownvitals.jobs.withings.lane_for``.
LANE_SYNC_NOW: Final = 1
LANE_WEBHOOK: Final = 2
LANE_POLL: Final = 3
LANE_RECONCILE: Final = 4
LANE_BACKFILL: Final = 5
LANES: Final = frozenset({LANE_SYNC_NOW, LANE_WEBHOOK, LANE_POLL, LANE_RECONCILE, LANE_BACKFILL})
DEFAULT_SLOT_CAP: Final = 4
DEFAULT_LEASE_SECONDS: Final = 30
BUDGET_PER_MINUTE: Final = 100

_CLAIM = text(
    "SELECT granted, lease_id, retry_at, reason FROM withings_rate_claim"
    "(:app, :account, :lane, :lease)"
)
_RELEASE = text("SELECT withings_rate_release(:lease)")
_PAUSE = text("SELECT withings_rate_pause(:app, :wait)")


@dataclass(frozen=True)
class Claim:
    """One attempt. A granted claim has spent a token.

    ``reason`` is ``granted``, ``paused``, ``empty``, ``busy`` or ``wait``.
    """

    granted: bool
    lease_id: UUID | None
    retry_at: datetime | None
    reason: str


def claim(
    connection: Connection,
    app_id: int,
    account_id: UUID,
    lane: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Claim:
    """Take one token and one slot, or say why not. Does not refund a token it grants."""
    if lane not in LANES:
        raise ValueError("lane must be 1 to 5")
    if lease_seconds < 1 or lease_seconds > 600:
        raise ValueError("lease_seconds must be 1 to 600")
    granted, lease_id, retry_at, reason = connection.execute(
        _CLAIM,
        {
            "app": app_id,
            "account": account_id,
            "lane": lane,
            "lease": lease_seconds,
        },
    ).one()
    return Claim(
        granted=bool(granted),
        lease_id=lease_id,
        retry_at=retry_at,
        reason=str(reason),
    )


def release(connection: Connection, lease_id: UUID) -> None:
    """Free the slot. The token stays consumed."""
    connection.execute(_RELEASE, {"lease": lease_id})


def pause(connection: Connection, app_id: int, wait_seconds: float) -> datetime:
    """Hold the app's bucket. The wait is clamped before it is stored."""
    resume = connection.execute(
        _PAUSE, {"app": app_id, "wait": clamp_rate_wait(wait_seconds)}
    ).scalar_one()
    if not isinstance(resume, datetime):
        raise RuntimeError("the rate pause did not return a timestamp")
    return resume


def claim_code_exchange(connection: Connection, app_id: int, account_id: UUID) -> Claim:
    """The web process's authorization-code exchange, on the user lane, spending one token."""
    return claim(connection, app_id, account_id, LANE_SYNC_NOW)
