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
   D9's "household accounts only": until the M8 gate every account is a household account, so
   the switch is the whole rule; the beyond-household rule lands with M8.
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
   module differs. Reason codes are codes, never display labels.

The API answers 404 for a finding the caller may not see (another user's, missing, hidden, or
experimental without the switch); the list defaults to the live tab (`origin = live`); history
is `origin = backfill`. Areas come from the rule's letter (B body, C and E heart, N nerve, A
activity, Q system). Text keys `finding.<rule>.why` and `finding.<rule>.isnt` carry "why you're
seeing this" and "what it isn't".

**Guard.** `tests/test_findings_read_guard.py` fails for any module of `src/ownvitals` other than
its `ALLOWED` list that names the `findings` table or the `estimated` column in SQL or Core. The
list, by name as ADR 0006 §11 allows: the read function, the two table definitions, the ingest
and `estimated` recompute writes (`ingest.rows`), `ecg_validate`, and the purge paths. The
rules engine's findings writer and the recompute sweep join it in their own PRs.
`tests/session_scan.py` allowlists `ownvitals.views.findings_read` (exact module) for
`user_session`.

**Not here.** The E4/E6 notification hold (the M6/M7 notification issue) calls `read_findings`
with `consumer="notification"`. The finding staleness of PLAN §5 is the writers'.

**Assumptions (unverified against Withings).** A7's link (same device and timestamp) is ADR 0006's
assumption until the owner's OC5; `evidence.trigger_groups` holds `grpid`s only, so a `grpid`
repeated across two connections of one user links both.

### Tests

`tests/ecg/test_overlay.py` and `test_overlay_counts.py` (every overlay case of the ADR 0006 test
specification; pure), `tests/test_views_findings.py` (the read function on the app role: the
switch, the overlay at read time, filters, readings, estimates, isolation with and without the
owner filter), `tests/test_views_findings_api.py` (the routes, two signed-in users) and
`tests/test_findings_read_guard.py`. The database tests need the test database of
[backend.md](backend.md).
