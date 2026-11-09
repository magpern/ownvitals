"""The Withings integration's metrics (PLAN section 4.4, "Observability").

Every metric is aggregated across users: labels are closed vocabularies (streams, job states,
outcomes), never a user id, email, token or measurement. ``ownvitals.metrics`` refuses any
other label value. Code records through the functions below; the DB-derived gauges are set from
the rows of ``metrics_withings_aggregates()`` (``apply_aggregates``: counts and ages, no ids),
which the worker reads in a system session (``ownvitals.worker.metrics``).
"""

from collections.abc import Iterable
from typing import Final

from ownvitals.metrics import REGISTRY, Counter, Gauge, Histogram
from ownvitals.withings.streams import STREAMS

STREAM_NAMES: Final = tuple(s.name for s in STREAMS)
# In queue_stats()'s order (20-queue.sql); tests/test_metrics_aggregates_db.py pins it.
JOB_STATES: Final = ("queued", "running", "completed", "failed", "dead_letter", "cancelled")
SUBSCRIPTION_STATES: Final = ("active", "pending", "failed", "missing")
RECONCILE_KINDS: Final = ("changed", "deleted")
REFRESH_RESULTS: Final = ("failure", "recovered")
COMPLETENESS_STATES: Final = ("complete", "partial", "not_available", "backfilling")
AGE_BUCKETS: Final = (1, 5, 30, 60, 300, 900, 3600, 21600, 86400)

webhook_delivery_age = REGISTRY.register(
    Histogram(
        "ownvitals_withings_webhook_delivery_age_seconds",
        "Webhook received time minus the notification's enddate.",
        AGE_BUCKETS,
    )
)
last_sync_success = REGISTRY.register(
    Gauge(
        "ownvitals_withings_last_sync_success_age_seconds",
        "Age of the oldest last successful sync per stream, across connections.",
        {"stream": STREAM_NAMES},
    )
)
subscriptions = REGISTRY.register(
    Gauge(
        "ownvitals_withings_subscriptions",
        "Webhook subscriptions by health state.",
        {"state": SUBSCRIPTION_STATES},
    )
)
reconcile_rows = REGISTRY.register(
    Counter(
        "ownvitals_withings_reconcile_rows_total",
        "Rows changed or deleted by reconciliation (drift).",
        {"kind": RECONCILE_KINDS},
    )
)
queue_age = REGISTRY.register(
    Gauge(
        "ownvitals_jobs_oldest_queued_age_seconds",
        "Age of the oldest queued job.",
    )
)
queue_jobs = REGISTRY.register(
    Gauge("ownvitals_jobs", "Jobs per state, dead letters included.", {"state": JOB_STATES})
)
token_refresh = REGISTRY.register(
    Counter(
        "ownvitals_withings_token_refresh_total",
        "Token refresh failures and recoveries.",
        {"result": REFRESH_RESULTS},
    )
)
api_calls = REGISTRY.register(
    Counter("ownvitals_withings_api_calls_total", "Withings API calls (quota use).")
)
api_rate_limited = REGISTRY.register(
    Counter("ownvitals_withings_api_601_total", "Withings answers with status 601 (rate limit).")
)
stream_completeness = REGISTRY.register(
    Gauge(
        "ownvitals_withings_stream_completeness",
        "Connections per stream and completeness state.",
        {"stream": STREAM_NAMES, "state": COMPLETENESS_STATES},
    )
)


def record_webhook(received_at: float, enddate: float) -> None:
    """Epoch seconds. A negative age (clock skew) is recorded as 0."""
    record_webhook_age(received_at - enddate)


def record_webhook_age(age_seconds: float) -> None:
    """One delivery age, as ``metrics_webhook_ages()`` returns it. Negative is recorded as 0."""
    webhook_delivery_age.observe(max(0.0, age_seconds))


def record_last_sync_age(stream: str, age_seconds: float) -> None:
    last_sync_success.set(max(0.0, age_seconds), stream=stream)


def set_subscription_counts(counts: dict[str, int]) -> None:
    for state in SUBSCRIPTION_STATES:
        subscriptions.set(counts.get(state, 0), state=state)


def record_reconcile(*, changed: int = 0, deleted: int = 0) -> None:
    if changed:
        reconcile_rows.inc(changed, kind="changed")
    if deleted:
        reconcile_rows.inc(deleted, kind="deleted")


def set_queue(stats: dict[str, int], oldest_queued_age_seconds: float = 0.0) -> None:
    """``stats`` is ``queue_stats()``'s ``metric -> total``: keys ``jobs_<state>``, no ids."""
    for state in JOB_STATES:
        queue_jobs.set(stats.get(f"jobs_{state}", 0), state=state)
    queue_age.set(max(0.0, oldest_queued_age_seconds))


def record_refresh_failure() -> None:
    token_refresh.inc(result="failure")


def record_refresh_recovery() -> None:
    token_refresh.inc(result="recovered")


def record_api_call(*, status: int | None = None) -> None:
    api_calls.inc()
    if status == 601:
        api_rate_limited.inc()


def set_completeness(stream: str, counts: dict[str, int]) -> None:
    for state in COMPLETENESS_STATES:
        stream_completeness.set(counts.get(state, 0), stream=stream, state=state)


Aggregate = tuple[str, str | None, str | None, float]


def apply_aggregates(rows: Iterable[Aggregate]) -> float:
    """Set the DB-derived gauges from ``metrics_withings_aggregates()`` rows ``(metric, stream,
    state, total)``; return the oldest due queued job's age for ``set_queue``. A stream without
    a successful sync has no age series; one without cursors counts zero in every state. A
    stream, state or metric outside the closed sets is skipped, never a new series."""
    ages: dict[str, float] = {}
    health: dict[str, int] = {}
    completeness: dict[str, dict[str, int]] = {}
    oldest_queued = 0.0
    for metric, stream, state, total in rows:
        if metric == "last_sync_age" and stream in STREAM_NAMES:
            ages[stream] = total
        elif metric == "subscriptions" and state in SUBSCRIPTION_STATES:
            health[state] = int(total)
        elif metric == "completeness" and stream in STREAM_NAMES and state in COMPLETENESS_STATES:
            completeness.setdefault(stream, {})[state] = int(total)
        elif metric == "queue_oldest_age":
            oldest_queued = total
    last_sync_success.reset()
    for stream, age in ages.items():
        record_last_sync_age(stream, age)
    set_subscription_counts(health)
    for stream in STREAM_NAMES:
        set_completeness(stream, completeness.get(stream, {}))
    return oldest_queued
