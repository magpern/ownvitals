# Metrics: Prometheus for the Withings integration

PLAN section 4.4 "Observability", issues #28 and #167. `backend/src/ownvitals/metrics.py` is a
small registry (counters, gauges, histograms; no client library, ADR 0009 note of 2026-11-09)
with the text exposition format; `backend/src/ownvitals/withings/metrics.py` defines the metrics
and the functions that record them; `backend/src/ownvitals/metrics_server.py` is the worker's
`/metrics` listener and `backend/src/ownvitals/worker/metrics.py` starts it and refreshes the
database-derived gauges. IMPLEMENTED against synthetic data only.

## Metrics

| Metric | Type | Labels | Moved by |
|---|---|---|---|
| `ownvitals_withings_webhook_delivery_age_seconds` | histogram | none | the worker's refresh: `metrics_webhook_ages()` (received minus `enddate`) |
| `ownvitals_withings_last_sync_success_age_seconds` | gauge | `stream` | the refresh: `metrics_withings_aggregates()` |
| `ownvitals_withings_subscriptions` | gauge | `state` | the refresh: `metrics_withings_aggregates()` |
| `ownvitals_withings_reconcile_rows_total` | counter | `kind` (changed, deleted) | `reconcile.py` (`record_reconcile` per stream pass) |
| `ownvitals_jobs_oldest_queued_age_seconds`, `ownvitals_jobs` | gauge | `state` | the refresh: `queue_stats()` and `metrics_withings_aggregates()` |
| `ownvitals_withings_token_refresh_total` | counter | `result` (failure, recovered) | `tokens.py`: a `requesttoken` that stores no pair, a recovery that stores one |
| `ownvitals_withings_api_calls_total`, `ownvitals_withings_api_601_total` | counter | none | `ingest/calls.py` (`limited`): each granted rate-limited call, and each 601 |
| `ownvitals_withings_stream_completeness` | gauge | `stream`, `state` | the refresh: `metrics_withings_aggregates()` |

The webhook receiver runs in web, which serves no metrics: the worker reads the delivery ages
the receiver stored (`webhook_events.received_at` and `enddate`) instead, each once.

## Label safety

Label names must be on `ALLOWED_LABELS`; every label declares a **closed set of values** and any
other value raises `LabelError`. A user id, email, token or measurement therefore cannot become a
series. `tests/test_metrics.py` fails if a registered metric has a label off the list or an open,
identifier-shaped or numeric value. Metrics are aggregates across users: counts and ages only. A
stream, state or metric from the database outside the closed sets is skipped, never a new
series (`apply_aggregates`); a test plants identifier-shaped values and checks they never
reach the output.

## The listener

`metrics_bind` (`OWNVITALS_METRICS_BIND`, default unset: no listener) is a loopback or private IP
literal; `metrics_port` (`OWNVITALS_METRICS_PORT`, default 9464) its port. The worker refuses to
start (exit code 2) with any other bind: a host name, a wildcard, `0.0.0.0/8`, broadcast,
link-local, IPv4-mapped or 6to4 IPv6, a zone id or a public address. It answers `GET /metrics`
only (400 for a request or header line over 4096 bytes or more than 64 headers, 405 for another
method, 404 for another path), drops a client that sends nothing after 5 s, runs each exchange
(`drain()` included) under a 10 s deadline, serves at most 4 connections at a time and closes
one more at once. A client that resets is not an error. It is never routed through SWAG:
Prometheus on the home server scrapes the worker's internal address. It is the one server socket
outside `ownvitals.egress` (ADR 0009, note of 2026-11-09; `tests/test_egress_guard.py`).

## The database aggregates

`metrics_refresh_seconds` (default 30) apart, the worker reads in a system session
`queue_stats()` and the two `SECURITY DEFINER` functions of
`docker/postgres/withings/40-withings.sql`, owned by the NOLOGIN, BYPASSRLS, read-only role
`ownvitals_metrics` (column `SELECT` only, on the columns they read; pinned by digest in
`ownvitals/db/queue_digests.py` and admitted by name in `20-queue.sql`). Both refuse a user
session (`app.user_id` set) and return counts and ages only:

- `metrics_withings_aggregates()`: per stream, the age of the oldest last successful sync over
  connected connections; subscription health per live connection; per-stream completeness over
  connected connections; the age of the oldest due queued job.
- `metrics_webhook_ages(after)`: the delivery ages of notifications received after `after`
  (at most 1000, oldest first). The worker starts at its own start time, so a restart does not
  replay history; a notification committed late with an earlier received time is not counted.

Definitions (documented assumptions, reversible; the owner's first real deployment shows whether
they read well):

| Gauge | State | Means |
|---|---|---|
| subscriptions | `failed` | the connection needs a reconnect: its subscriptions cannot be repaired |
| | `pending` | connected and marked for resubscribe (the nightly repair has not run yet) |
| | `active` | connected and a notification arrived within 7 days |
| | `missing` | connected and none did (a lost subscription, or a quiet account) |
| completeness | `not_available` | not shared, unsupported or not available |
| | `complete` | the backfill is complete |
| | `backfilling` | the backfill is running |
| | `partial` | anything else (an import that stopped, or none yet) |
