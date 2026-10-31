"""Jobs (ADR 0005): the job issuer of ``UserContext``, kinds, the fence, transitions and the queue
calls; the jobs API and its event stream for web. The worker loop (``ownvitals.worker.loop``)
drives the worker side; M1 registers the Withings kinds and adds origins, lanes, singleton keys,
the rate-limit hold and dead-letter (ADR 0005 section 8)."""

from ownvitals.jobs.errors import (
    CATEGORIES,
    RETRYABLE,
    JobFailure,
    LeaseLost,
    RunStopped,
    backoff_seconds,
    failure_of,
)
from ownvitals.jobs.fence import FencedSession, Lease, fenced_session
from ownvitals.jobs.issuer import NO_ACCOUNTS, Accounts, Claimed
from ownvitals.jobs.kinds import KINDS, Complete, JobKind, KindRegistry, Outcome
from ownvitals.jobs.run import JobRun

__all__ = [
    "CATEGORIES",
    "KINDS",
    "NO_ACCOUNTS",
    "RETRYABLE",
    "Accounts",
    "Claimed",
    "Complete",
    "FencedSession",
    "JobFailure",
    "JobKind",
    "JobRun",
    "KindRegistry",
    "Lease",
    "LeaseLost",
    "Outcome",
    "RunStopped",
    "backoff_seconds",
    "failure_of",
    "fenced_session",
]
