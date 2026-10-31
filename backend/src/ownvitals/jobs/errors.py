"""Failure categories, retry classes and backoff of a job (ADR 0005 section 6).

A kind's code fails a run by raising ``JobFailure(category)``; anything else it raises is mapped
here (a transient error of our own database to ``DATABASE_RETRY_CATEGORY``, any other exception
to ``internal``). Only the category is ever kept: no exception text reaches a job row, an event or
a log. M1 maps the Withings client's errors and the egress refusal (``outbound_blocked``) here.
"""

import random
from typing import Final

from sqlalchemy.exc import DisconnectionError, OperationalError

# ADR 0005 section 6; the same list as the ``jobs.error_category`` check of migration 0003.
CATEGORIES: Final = (
    "upstream_unavailable",
    "rate_limited",
    "lease_lost",
    "not_connected",
    "needs_reconnect",
    "key_unavailable",
    "decrypt_failed",
    "invalid_input",
    "account_disabled",
    "outbound_blocked",
    "internal",
)
# Retried by the worker (a new attempt with backoff). ``lease_lost`` is retried by the reaper.
RETRYABLE: Final = frozenset({"upstream_unavailable", "rate_limited"})

# A transient error of our own database (connection lost, a statement or lock timeout) during a
# checkpoint, a fenced write or the store of a result: ADR 0005 section 6 retries it as
# ``upstream_unavailable`` ("also our own database's transient errors").
DATABASE_RETRY_CATEGORY: Final = "upstream_unavailable"

BACKOFF_BASE_SECONDS: Final = 30.0
BACKOFF_CAP_SECONDS: Final = 900.0  # 15 minutes
RATE_LIMIT_CAP_SECONDS: Final = 3600.0  # a hint is honoured up to an hour
JITTER: Final = 0.2  # +-20 %


class JobFailure(Exception):
    """A run failed with ``category``. ``args == (category,)``; ``retry_after`` (seconds) only for
    ``rate_limited``. An unknown category becomes ``internal``."""

    def __init__(self, category: str, retry_after: float | None = None) -> None:
        if category not in CATEGORIES:
            category = "internal"
        super().__init__(category)
        self.category = category
        self.retry_after = retry_after if category == "rate_limited" else None


class LeaseLost(Exception):
    """The fence matched no row: this worker no longer holds the job (ADR 0005 section 4).
    The transaction was rolled back; the run stops and writes nothing more."""


class RunStopped(Exception):
    """A checkpoint stopped the run: ``reason`` is ``cancelled``, ``lost`` or ``shutdown``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def failure_of(exc: BaseException) -> JobFailure:
    """The ``JobFailure`` for anything a kind's code raised (never its text)."""
    if isinstance(exc, JobFailure):
        return exc
    if isinstance(exc, OperationalError | DisconnectionError):
        return JobFailure(DATABASE_RETRY_CATEGORY)
    return JobFailure("internal")


def backoff_seconds(
    attempt: int, retry_after: float | None = None, rng: random.Random | None = None
) -> float:
    """``min(30 s * 2^(attempt-1), 15 min)`` with +-20 % jitter; for a rate limit at least its
    hint, capped at an hour. ``attempt`` is the attempt that just failed (1 for the first)."""
    base = min(BACKOFF_BASE_SECONDS * 2 ** (max(attempt, 1) - 1), BACKOFF_CAP_SECONDS)
    delay = base * (1 + (rng or random).uniform(-JITTER, JITTER))
    if retry_after is not None and retry_after > 0:
        delay = max(delay, min(float(retry_after), RATE_LIMIT_CAP_SECONDS))
    return min(delay, RATE_LIMIT_CAP_SECONDS)
