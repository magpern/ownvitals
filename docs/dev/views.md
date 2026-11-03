# Views: what outputs read

`ownvitals.views` holds the read models for presentation. Each output (the dashboard and API,
notifications, the JSON/CSV export, the doctor PDF, the AI context) reads through them, never
the tables.

## Read API: status, metrics, days, readings, nerve summary (#189)

PLAN §7.1 and §8, the design's data states and information architecture, ADR 0010 §2 and §8.
Every route needs a full session and is read as the signed-in user under forced RLS (one
`user_session`, every statement also filters `owner_id`); every answer is `no-store`.

| Route | What |
|---|---|
| `GET /api/status` | The freshness line: `last_reading_at`, `last_sync_at`, `connection` (`not_connected`, `connected`, `needs_reconnect`, `withings_plus_needed`), one entry per stream (`state`, `capability`, `progress_percent`, `last_ok_at`), `importing`, `incomplete`, `broken_connections` |
| `GET /api/metrics` | Every metric of `analysis.quality.METRICS` with `state`, `progress` (insufficient data), `latest`, `typical_range` (display band, k 1.5, 90 days) and a 30-day `sparkline` (`null` on a day without a value) |
| `GET /api/metrics/{key}?range=30d\|90d\|6mo\|1y\|all` | The daily `series` (every day of the window; a gap is `value: null, n: 0`), `unconfirmed` days, `band`, Theil-Sen `trend`, `change_points`, `data_quality`, `method` (text, parameters, algorithm versions) and `provenance` (`recompute_pending`) |
| `GET /api/days/{key}/{day}` | The day's daily value, its readings (id, time in the reading's zone, value, flags, `used`) and the other metrics recorded that day |
| `GET /api/readings/{id}` | The drawer: `value_raw x 10^unit_exponent`, `attrib`, device model/type/firmware, source zone, the four quality gates, `used_in_daily`, provenance (`algo_version`, `input_revision`, `input_fingerprint` hex) |
| `GET /api/nerve/summary?months=12` | The nerve metrics' state (N1's minimum-data progress) and, per month (newest first), days with data, median, lowest and highest value of each, and the N2/N3 findings whose window ended that month |

| Path | What |
|---|---|
| `views/reads.py` | `reading()`: the one session of the routes above (`Reader`: `status_facts`, `daily`, `day_readings`, `reading`, ...). The exact module is allowlisted in `tests/session_scan.py` next to `findings_read`; the connection row comes through `connections.repository` (only that package holds SQL on the table) |
| `views/states.py` | Pure: the data states and their precedence, import percentage, progress toward a requirement |
| `views/assemble.py` | Pure: the tile, the detail, band, trend and change points, called as the finding rules call them |
| `views/catalog.py` | Pure: areas, the stream of each metric, ranges, the stale threshold |
| `views/schemas.py`, `*_routes.py`, `deps.py` | Response models, routes, the clock dependency (`view_now`, overridden in tests) |

**Data states** (`states.py`, one wording everywhere): the stream's first, in this order: no live
connection (`not_connected`), `needs_reconnect`, the Withings+ block (`withings_plus_needed`: the
connection's or a cursor's `blocked_by_provider_plan`), the capability the sync learned
(`not_authorized` is `not_shared`, `unsupported` is `not_supported`, `not_available`), the import
(`importing n %` while a `withings_backfill` job is queued or running, otherwise `incomplete`),
then freshness (`stale` when the last successful sync is more than 48 hours old). A metric adds
`insufficient_data` below the baseline policy (with `progress`: days with data and span against the
requirement) and `stale` while a daily value's `input_revision` is behind the stream's counter
(the value shown is the last good one). A blocked stream never reads as "no data": its state says
what it is, and no count, band or progress is given for it. The ECG intervals (135 to 138) take
their capability from the `ecg` stream, the rest from `measures`.

**Gaps and zeros.** A day without a value is `null` with `n: 0` in the series and the sparkline,
never `0`. A band, trend or change points exist only for a series that passes the minimum-data
policy of its window (the metric's policy: trend 8 days spanning 60 % of the window, baseline 10
days over 21, nerve 4/10 over 28); otherwise `insufficient_data` with its progress. Change points
and trend use the chart's window, so a 6-month window needs more span than a 90-day one.

**Experimental.** No metric value here is experimental. The only experimental values are findings,
and they come through `findings_read` (the nerve summary), so they are returned only when the
owner-only switch is effective for the caller.

**No Withings ids.** No response has a group id, hashed device id, Withings user id, connection
id or token; `tests/test_views_days.py` greps every route's answer and the log for planted ones.
A reading id is the `observations` row's own uuid; another user's id, day or metric answers 404,
as does a key outside the catalog and an observation of another stream (sleep, activity).

**Assumptions (unverified against Withings).**

- `withings_plus_needed` has no known provider signal (PLAN §4.2, `capabilities.provider_plan_blocked`
  is false for every status): the state is shown when the connection's `last_error` or a cursor's
  `last_error_category` is `blocked_by_provider_plan`, which nothing writes yet. A test plants it.
- A stream is `stale` after 48 hours without a successful sync (the daily poll is the safety net
  behind the webhooks); that threshold is ours.
- Importing versus incomplete is a queued or running `withings_backfill` job for the stream; a
  stalled import without a job reads `incomplete`.
- Day assignment is the reading's own zone (derived.md); a profile `fixed_tz` is not applied yet.

Tests: `tests/test_views_states.py` (pure), `test_views_status_metrics.py` (every state, from
synthetic groups through the real ingest, normalisation and daily passes), `test_views_metric_detail.py`,
`test_views_days.py` (RLS 404s, drawer, redaction), `test_views_nerve.py` (the experimental switch).
Planting helpers: `tests/viewtools.py`. They need the test database of [backend.md](backend.md).

## Findings: the one read function (#190)

ADR 0006 §§9 and 11, ADR 0010, PLAN §7.2 and D9.

| Path | What |
|---|---|
| `backend/src/ownvitals/ecg/overlay.py` | Pure: the A7 link (`a7_links`, `resolve_links`), the gate result of one `estimated` (`gate_result`), `ecg_gate_overlay`, and §9's up-to-date rule (`estimate_view`) |
| `backend/src/ownvitals/views/findings_read.py` | `read_findings`, `read_finding`, `read_ecg_estimates`: the only module that reads `findings` or `estimated` for presentation, and the only one in `views` that opens a `user_session` |
| `backend/src/ownvitals/views/findings_routes.py` | `GET /api/findings?tab=live\|history&tier=&area=` and `GET /api/findings/{id}` |

Each call is one `user_session` (forced RLS); every statement also filters `owner_id`. Then:

1. **Experimental tier.** Loaded only when `flags.effective.experimental_findings` is on (the
   environment's `OWNVITALS_EXPERIMENTAL_FINDINGS` and the stored `experimental_findings`; default
   off). Without it no `tier = 'experimental'` row is loaded, and a row whose `rule_id` is an
   experimental rule (ADR 0010's B1, B3, C1x; ADR 0006's E4, E5) is dropped whatever its tier.
   D9's "household accounts only": see "Decisions" below.
2. **Overlay.** The finding's `evidence.trigger_groups` are linked now (A7: same connection and
   timestamp, an ECG type 130 or 135–138 in the group, same device; a soft-deleted group or a
   deleted recording is no link). E4 and E6: hidden when at least half of the counted trigger
   recordings failed our gate; `not_evaluated` when none has a gate result. E1–E3: never hidden.
   Other rules: no overlay. The gate outcome and counts (`gate`) are given only behind the
   switch, for every rule; the `not_evaluated` mark on E4 and E6 is given to everyone who sees
   the finding.
3. **Consumer.** `dashboard`, `notification`, `doctor_pdf` and `ai_context` never receive a
   hidden finding; `export` receives every finding with `hidden` set.
4. **Estimates.** Only behind the switch, through `estimate_view`: HR and HRV only from an
   `estimated` whose `algo_version` and `input_fingerprint` are current; R-peaks, SQI windows
   and gate reasons also on an `hrv`-only difference; nothing when the fingerprint or a gate
   module differs. Reason codes are codes, never display labels. The API marks each estimate
   `source: "ownvitals_estimate"` with `label_key: "ecg.estimate.label"`: the page shows it in a
   panel of its own under §9's label ("Estimated by ownvitals from the raw signal; not a Withings
   measurement"), never in a Withings slot.

The API answers 404 for a finding the caller may not see (another user's, missing, hidden, or
experimental without the switch); the list defaults to the live tab (`origin = live`); history
is `origin = backfill`. The list pages by `limit` (default 50, **at most 200**) and `offset`,
newest `last_seen` first; `next_offset` is null on the last page. The experimental and area
filters run in SQL before the page is cut; a hidden finding is dropped after it, so a page can
hold fewer than `limit` findings. The read function's other consumers read every row
(`limit=None`). Areas come from the rule's letter (B body, C and E heart, N nerve, A
activity, Q system). Text keys `finding.<rule>.why` and `finding.<rule>.isnt` carry "why you're
seeing this" and "what it isn't".

**Guard (structural).** `tests/test_findings_read_guard.py`: outside its `ALLOWED` modules (exact
names), no module of `src/ownvitals` holds a string with `findings`, `estimated` or
`ecg_recordings` as a word (any case; every string constant, docstrings aside: concatenation
parts, `%` and `.format` templates and arguments, f-string pieces), imports a name `findings` or
`ecg_recordings` from any module (or `*`), reaches `.findings`/`.ecg_recordings` on an imported
module, or names `.c.estimated`/`.columns.estimated`. So SQL, whole-row reads of
`ecg_recordings`, JSONB access to `estimated` and Core spellings are all refused, not matched one
by one. A few strings were reviewed as no read (`REVIEWED_STRINGS`: a citation in
`analysis.quality`, the Findings route key and path in `notify.outbox`, the API prefix), each
pinned with its number of occurrences. `ALLOWED`, by name as ADR 0006 §11 allows: the read
function, the two table definitions (`derived.tables`, `withings.tables`), `ingest.rows` (ingest
and the `estimated` recompute writes), `ecg_validate` (#47) and `connections.purge` (delete
imported data). Every entry must still name a table (the exactness test). The rules engine's
findings writer and the recompute sweep join it in their own PRs.

**Database side (N4).** No view, materialized view, rule, function or procedure may read these
tables outside the read function: the guard refuses a migration string that creates one naming
`findings`, `estimated` or `ecg_recordings`, and any SQL file under `docker/postgres` that names
them (a `SECURITY DEFINER` function there would read past RLS). None exists today; one would
extend the guard in its own PR, with its reason.
`tests/session_scan.py` allowlists `ownvitals.views.findings_read` (exact module) for
`user_session`.

**Not here.** The E4/E6 notification hold (the M6/M7 notification issue) calls `read_findings`
with `consumer="notification"`. The finding staleness of PLAN §5 is the writers'.

**Decisions (N3 of the #228 review; the Architect mirrors them in `docs/status.md`).**

- **Household rule = the switch alone until M8.** D9 shows the experimental tier only on
  household accounts with the owner-only flag. Until the M8 gate every account is a household
  account (`open` sign-up is refused), so `experimental_findings` is the whole rule. M8 must add
  the per-account household check before any non-household account exists.
- **A `grpid` repeated across connections links both.** `evidence.trigger_groups` holds
  `grpid`s only, and `measure_groups` is unique per `(owner, connection, grpid)`; a `grpid` that
  two connections of one user both hold resolves to both groups, and each links by A7. A user
  has at most one live Withings connection, so this needs a disconnected connection that kept
  its rows. Revisit if evidence gains the connection.

**Assumptions (unverified against Withings).** A7's link (intervals 135–138 and the AFib result
130 share device and timestamp with the ECG) is ADR 0006's assumption until the owner's OC5.

### Tests

`tests/ecg/test_overlay.py` and `test_overlay_counts.py` (every overlay case of the ADR 0006 test
specification; pure), `tests/test_views_findings.py` (the read function on the app role: the
switch, the overlay at read time, filters, readings, estimates, isolation with and without the
owner filter), `tests/test_views_findings_api.py` (the routes, two signed-in users) and
`tests/test_findings_read_guard.py`. The database tests need the test database of
[backend.md](backend.md).
