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

## Endpoint

`serve_metrics(host, port)` answers `GET /metrics` only (404 and 405 otherwise) and refuses to
bind to anything but a loopback or private IP literal (`BindRefused` for `0.0.0.0`, `::`, public
addresses and names). It is for the home server's Prometheus and is never routed through SWAG.

## Not wired yet (follow-ups)

The call sites live in files this issue does not own: the webhook receiver, sync, reconcile,
token refresh, the rate limiter and the worker loop (where `serve_metrics` would start and the
aggregate gauges would be refreshed). Cross-user aggregates (last sync per stream, subscription
health, completeness) need reviewed `SECURITY DEFINER` functions (AGENTS.md); only
`queue_stats()` exists today.
