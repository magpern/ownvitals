"""The Withings integration's metrics (PLAN section 4.4, "Observability").

Every metric is aggregated across users: labels are closed vocabularies (streams, job states,
outcomes), never a user id, email, token or measurement. ``ownvitals.metrics`` refuses any
other label value. Code records through the functions below; the DB-derived gauges are set from
aggregates the caller computed (counts and ages, no ids).
"""

from typing import Final

from ownvitals.metrics import REGISTRY, Counter, Gauge, Histogram
from ownvitals.withings.streams import STREAMS

STREAM_NAMES: Final = tuple(s.name for s in STREAMS)
JOB_STATES: Final = ("queued", "running", "completed", "failed", "cancelled", "dead_letter")
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
    webhook_delivery_age.observe(max(0.0, received_at - enddate))


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
    """``stats`` is ``queue_stats()``'s ``metric -> total`` (state names, no ids)."""
    for state in JOB_STATES:
        queue_jobs.set(stats.get(state, 0), state=state)
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
