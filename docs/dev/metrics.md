# Metrics: Prometheus for the Withings integration

PLAN section 4.4 "Observability", issue #28. `backend/src/ownvitals/metrics.py` is a small
registry (counters, gauges, histograms; no client library) with the text exposition format and
the internal endpoint; `backend/src/ownvitals/withings/metrics.py` defines the metrics and the
functions that record them. IMPLEMENTED against synthetic data only.

## Metrics

| Metric | Type | Labels | Recorded by |
|---|---|---|---|
| `ownvitals_withings_webhook_delivery_age_seconds` | histogram | none | `record_webhook(received_at, enddate)` |
| `ownvitals_withings_last_sync_success_age_seconds` | gauge | `stream` | `record_last_sync_age` |
| `ownvitals_withings_subscriptions` | gauge | `state` | `set_subscription_counts` |
| `ownvitals_withings_reconcile_rows_total` | counter | `kind` (changed, deleted) | `record_reconcile` |
| `ownvitals_jobs_oldest_queued_age_seconds`, `ownvitals_jobs` | gauge | `state` | `set_queue` (`queue_stats()` output) |
| `ownvitals_withings_token_refresh_total` | counter | `result` (failure, recovered) | `record_refresh_failure`, `record_refresh_recovery` |
| `ownvitals_withings_api_calls_total`, `ownvitals_withings_api_601_total` | counter | none | `record_api_call(status=...)` |
| `ownvitals_withings_stream_completeness` | gauge | `stream`, `state` | `set_completeness` |

## Label safety

Label names must be on `ALLOWED_LABELS`; every label declares a **closed set of values** and any
other value raises `LabelError`. A user id, email, token or measurement therefore cannot become a
series. `tests/test_metrics.py` fails if a registered metric has a label off the list or an open,
identifier-shaped or numeric value. Metrics are aggregates across users: counts and ages only.

## No endpoint here

This module is pure: it opens no socket (the egress guard forbids server sockets outside
`ownvitals.egress`). The scrape endpoint, its wiring and the DB aggregates are #167: the listener
design (bind refusal including IPv4-mapped, 6to4 and link-local addresses, a read timeout, a
4096-byte request-line limit and a connection cap, each with tests) moves there. Job-state
gauges take `queue_stats()` output (`jobs_<state>` keys); a test pins the states against
`docker/postgres/queue/20-queue.sql`.

## Not wired yet (#167)

Recorders are not called from the webhook receiver, sync, reconcile, token refresh or the rate
limiter, and nothing serves the registry. Cross-user aggregates (last sync per stream,
subscription health, completeness) need reviewed `SECURITY DEFINER` functions (AGENTS.md); only
`queue_stats()` exists today.
