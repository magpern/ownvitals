"""The access token a sync job calls Withings with (PLAN section 4.3).

A Withings access token lives 3 hours. The stored pair holds its expiry, and every job that
opens the grant (``sync_kinds.open_materials``) first asks ``fresh_access_token``: a token that
expires within ``REFRESH_WINDOW`` (or whose expiry is unknown) is replaced through
``refresh_tokens``, the crash-safe rotation of ``tokens.py``. An ambiguous result gets the one
recovery try of PLAN section 4.3 step 4. Jobs of one connection that race take the refresh lock;
the one that finds the generation moved returns the stored token and does not call Withings.

Without this step the first sync after the token's 3 hours failed with ``needs_reconnect`` and
nothing ever refreshed it. The outcomes map to job categories as ``worker.kinds`` maps the
recovery job's.
"""

from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from ownvitals.crypto.keks import KeyRing
from ownvitals.jobs import JobFailure
from ownvitals.jobs.run import JobRun
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.tokens import recover_refresh, refresh_tokens

REFRESH_WINDOW: Final = timedelta(minutes=10)
# One ``requesttoken`` (30 s) plus the refresh lock and the fenced writes around it, twice.
DEADLINE_SECONDS: Final = 120.0


def needs_refresh(expires_at: datetime | None, now: datetime) -> bool:
    """True when the token has expired, expires within the window, or its expiry is unknown."""
    return expires_at is None or expires_at - now <= REFRESH_WINDOW


async def fresh_access_token(
    job: JobRun,
    ring: KeyRing,
    connection_id: UUID,
    app: WithingsApp,
    *,
    token: str,
    expires_at: datetime | None,
) -> str:
    """``token`` while it is good for a while, else the refreshed one. Raises ``JobFailure``
    with ``needs_reconnect`` or ``not_connected`` (the grant is gone) or ``upstream_unavailable``
    (Withings did not answer; the next pass tries again)."""
    if not needs_refresh(expires_at, datetime.now(UTC)):
        return token
    async with WithingsClient.open(app) as client, job.provider_call(DEADLINE_SECONDS):
        result = await refresh_tokens(
            job.factory, ring, job.ctx, connection_id, client, lease=job.lease
        )
        if result.outcome == "recover":
            result = await recover_refresh(
                job.factory, ring, job.ctx, connection_id, client, lease=job.lease
            )
    if result.outcome in ("refreshed", "current") and result.access_token:
        return result.access_token
    if result.outcome in ("needs_reconnect", "not_connected"):
        raise JobFailure(result.outcome)
    raise JobFailure("upstream_unavailable")
