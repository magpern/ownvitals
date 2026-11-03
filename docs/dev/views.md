# Views: what outputs read

`ownvitals.views` holds the read models for presentation. Each output (the dashboard and API,
notifications, the JSON/CSV export, the doctor PDF, the AI context) reads through them, never
the tables.

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
