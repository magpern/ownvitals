# Reports: the JSON/CSV export

How a user takes their complete data out of ownvitals ([ADR 0013](../adr/0013-reports-and-export.md)
sections 1, 2, 4 and 5; PLAN §6, §11 M7; #211). The doctor PDF (ADR 0013 §6) and FIT output (§3)
come with their own issues and extend this file. The ECG PDF of `reports/ecg_pdf.py` is
[ecg.md](ecg.md)'s.

## The pieces

| Path | What |
|---|---|
| `backend/migrations/versions/0022_exports.py` | `exports` and `export_chunks` (owned, forced RLS, `owner_isolation`; chunks cascade from their export) |
| `backend/src/ownvitals/reports/tables.py` | the Core tables and vocabularies (`test_reports_export_db.py` holds them to the constraints) |
| `backend/src/ownvitals/reports/export_allowlist.py` | what is exported, per owned table and column, and what is not, with reasons (pure) |
| `backend/src/ownvitals/auth/exports.py` | `export_account` (the account tables' allowlisted columns) and `record_event` (the export audit kinds, typed details); guard 8's entry points |
| `backend/src/ownvitals/reports/export_format.py` | JSON and CSV of one row, the CSV-injection rule, the real value (pure) |
| `backend/src/ownvitals/reports/export_archive.py` | the zip as a stream of 4 MiB chunks, per-file SHA-256 and row counts, the size limit |
| `backend/src/ownvitals/reports/export_read.py`, `export_ecg.py` | pages of the owned rows in the job's fenced session; the recordings with their decoded signal |
| `backend/src/ownvitals/reports/export_files.py` | the archive's files: tables in pages, the recordings and signal, findings with their overlay state, estimates |
| `backend/src/ownvitals/reports/export.py` | the `export_build` job kind (worker only) |
| `backend/src/ownvitals/reports/export_store.py`, `export_routes.py` | `/api/exports`: request, list, download; the tidy of expired and failed rows |
| `docker/postgres/exports/60-exports.sql` | the role `ownvitals_exports` and the definer function `exports_purge_expired()`, the cross-user purge (#333); idempotent, run as the superuser by `db-functions` after `50-ai.sql` |
| `backend/src/ownvitals/reports/export_purge.py` | the worker's call of the purge, from the hourly purge tick (`worker/upkeep.py`) |
| `frontend/src/components/reports/ExportCard.tsx`, `frontend/src/api/exports.ts` | "Export everything" in Settings → Your data |
| `e2e/specs/export.spec.ts` | step-up → export → download, for an account without data and for one connected to fake-withings' synthetic user (the archive is unzipped: manifest, account, the imported measure groups and measures, row counts match the manifest) |

## The flow

1. `POST /api/exports` behind `require_sensitive` (recent sign-in and a step-up, both within 10
   minutes). Under the owner's job-creation lock: tidy, refuse a second build
   (`export_in_progress`, 409) or a full job slot (`busy`, 429), take one slot of
   `EXPORT_ACCOUNT` (3 per 24 hours, fixed window, HMAC-ed key; 429 `rate_limited` with
   `Retry-After`), insert the `exports` row and its `export_build` job (origin `user`, input
   `{export_id}` only) in one transaction. Audit `export_requested`.
2. The worker claims the job and the issuer builds the owner's context from the claimed row.
   The build reads owned tables only in fenced sessions (forced RLS, and every statement filters
   `owner_id`), the account tables only through `ownvitals.auth.exports.export_account`, and the
   findings and estimates only through `views.findings_read` (consumer `export`). The archive
   streams into `export_chunks`; a retry deletes the chunks of the attempt before. Progress events
   are file counts. Done: the row is `ready` with size, SHA-256 and `expires_at` (now +
   `export_ttl_hours`, 24), audit `export_ready`. Over `export_max_bytes` (1 GiB): chunks
   deleted, the row `failed` with `export_too_large` and the largest file stem, audit
   `export_failed`.
3. `GET /api/exports/{id}/file`: refused with a small page (403, header `X-Ownvitals-Reason:
   download_same_origin_required`, a link to Settings) unless `Sec-Fetch-Site: same-origin`, then
   `require_stepup`, then under RLS (404 for another user's id). The attempt is counted before any
   byte (at most 3). Only the latest attempt reads chunks, and the last chunk is read in the
   transaction that spends the export, so of parallel attempts at most one gets the whole
   archive. The body streams from the chunks with `Content-Disposition: attachment`,
   `Cache-Control: no-store`, `Content-Type: application/zip`, `X-Content-Type-Options: nosniff`
   and `Content-Length`. With the last chunk the row becomes `expired` with `download_count` 1 and
   the chunks are deleted. Audit `export_downloaded` (attempt, completed); a build whose job ended
   without an archive is marked `build_failed` by the tidy and audited `export_failed` then.
   `completed` is true only once the server has sent the last chunk: the export is spent when
   that chunk is handed over (so of parallel attempts at most one gets it), and a client that
   drops during it has lost the export and requests a new one. A stream that cannot go on (a
   later attempt began, the export expired meanwhile) is aborted (`DownloadAborted`), so the
   connection breaks short of `Content-Length` and the browser reports a failed download
   (a filter on the `uvicorn.error` logger tries to drop uvicorn's "Exception in ASGI
   application" record of it, but that is best effort: under the real stack it does not catch
   every case, so a traceback can still be logged, and the tests only exercise the filter on a
   hand-built record. The body logs one line without an id). The SPA also refuses to save a
   body shorter than its `Content-Length`, unless the response has a `Content-Encoding` (a proxy
   compressed it; the header then counts encoded bytes).
4. **The mail.** The transaction that marks the export ready also queues one `export_ready` row
   in the owner's outbox (route `settings_data`, `/settings/data`; migration 0024). The text says
   only that something the user asked for is waiting, for a short time, and links to Settings:
   never to the file, never the export's id (T59). Web's sender sends it; the worker cannot.
   It is sent whatever `notify_email` says (that setting is about findings): the user asked for
   the export a moment before. The Swedish table holds the English text until the translation
   PR (`UNTRANSLATED_SV`).
5. **The purge** (`exports_purge_expired()`, hourly, every owner), exactly ADR 0013 §4's list:
   ready exports past `expires_at` become `expired`, the chunks of failed and expired exports
   are deleted (oldest export first), and `failed` and `expired` rows older than 90 days are
   deleted. It returns three counts only and writes nothing else: no audit row (its role holds
   nothing on `auth_events` or `jobs`), no build state. At most 100 exports a step and 1000
   chunks a call, and the worker calls again while a call did something (at most 10 times a
   tick). Export rows a download holds are skipped until the next call. The owner's own tidy
   still runs on every list, request and download: it fails ended builds and records
   `export_failed` and `export_expired` for what it changes.
6. The SPA fetches the file (same-origin) and saves it as a blob; the shared step-up dialog
   handles `step_up_required` and `reauth_required` for both actions.

## The archive

`ownvitals-export-<date>.zip`: `manifest.json` (format `ownvitals-export/1`, app version,
generation time, account id, every file with rows and SHA-256, `withheld`), and per table
`data/<table>.json` (an array of objects, canonical key order) and `csv/<table>.csv` (the same
rows, RFC 4180):

- `account` and `auth_events` (from `export_account`); every `GENERIC` table of the allowlist
  (profile, connections, devices, measure groups, measures with their real value, observations,
  daily values, sleep, activity, workouts, intraday, scores, AI consents and conversations);
- `ecg_recordings` (the signal as its leads in JSON; CSV without it) and `csv/ecg_signal.csv`
  (`recording_id, lead, index, microvolts`);
- `findings`: every finding the read function gives the `export` consumer, with
  `overlay_state` (`shown`, `hidden_gate_failed`, `not_evaluated`);
- `ecg_estimates` only for an account with the experimental tier, each row with `source`
  "Estimated by ownvitals from the raw signal; not a Withings measurement" (the manifest entry
  carries the same `label`). Without the tier, the experimental score kinds (`his`) are left out
  of `scores` too, and `withheld` lists `ecg_estimates`, `experimental_findings` and
  `experimental_scores` with `experimental_tier_not_enabled`. The read function's one answer on
  the switch decides all three.

Never: tokens, secrets, any hash, HMAC or ciphertext, job rows, rate-limit buckets, system
tables, another user's rows. CSV text cells starting with `=`, `+`, `-`, `@`, a tab or a carriage
return get a leading `'`; numbers are written as numbers.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `export_max_bytes` | 1 GiB | over it the build fails `export_too_large` and keeps nothing |
| `export_ttl_hours` | 24 | how long a ready archive can be downloaded |

## Tests

Real PostgreSQL ([backend.md](backend.md)), synthetic accounts only:

| File | Covers |
|---|---|
| `test_reports_export_api.py` | the `export_ready` mail (one row, Settings link only, none for a failed build or one that left `building` before the ready update); no traceback for an aborted download; the events in the activity list; a client gone during the last chunk audited not completed; a superseded attempt aborted; the request without recent auth, without step-up, with both; one build at a time; 3 a day; the list per user; a download streams once (headers, size, SHA-256) and is then refused; no export id in the logs; the download without step-up, cross-site (`cross-site`, `same-site`, `none`, missing) without counting, from another user (404); 3 attempts; expiry deletes the chunks; old rows go after 90 days; a build that ended without an archive; the size limit |
| `test_reports_export_contents.py` | two users with a row in every owned table: A's archive holds none of B's ids, email or events, and the other way round; exactly the allowlisted columns in JSON and CSV; no planted secret; the experimental tier with and without the switch, hidden findings marked |
| `test_reports_export_allowlist.py` | every owned and account table and column is classified; no binary column exported |
| `test_reports_export_format.py` | the CSV-injection rule, numbers untouched, round trips, canonical JSON, the chunked zip, the size limit |
| `test_reports_export_db.py` | the vocabularies, forced RLS, one build per owner, chunks only of own exports, the cascade |
| `test_reports_export_purge.py` | `exports_purge_expired()` for two owners (expiry, chunks, 90 days, what stays, ended builds left to the tidy), no audit row, counts only, no argument, bounded steps, a held row skipped, chunks oldest first; the app role alone may call it and cannot do it itself; guard 2, guard 9, the startup check and the digest; drift found and repaired; the Python layer and the worker's tick |

## Known gaps

- **FIT** (`export_fit`, ADR 0013 §3) is off by default and not built (#333 records it).
- **No per-session binding of the download.** The download is authorised by the session cookie
  and a step-up within 10 minutes, as ADR 0013 §4 states; it is not bound to the session that
  requested the export, so another session of the same user with a fresh step-up can download it
  too (#333 records it, nothing changes).
- **A dropped last chunk spends the export** (above): the trade for "of parallel attempts at most
  one gets the whole archive". The user requests a new export (3 a day).
- **System-written expiry and failure events.** An export the purge expires has no
  `export_expired` event, and a build whose job ended is failed (with `export_failed`) only by
  its owner's tidy, when they next open Settings, request or download; until then its chunks
  stay. Writing those events (or failing builds) across users from the definer would widen its
  role to `auth_events`, an account table, so it waits for an ADR 0013 amendment (#333 review,
  option b). Until then those events are not in the activity list for an owner who does not
  come back.
- A build whose job row is gone (not ended, missing) is not failed by the tidy; it keeps the
  owner's one build slot. Jobs are not deleted today, so this cannot happen yet.
- The UI shows the states of the mock-up button (export, preparing, ready with its size,
  downloaded, failed with the largest part); those beyond the mock-up need the owner's design
  check (#333).
- The export events are in the activity list's allowlist (`activity_model.SHOWN`, #333), but no
  screen shows that list yet; the export events the system caused (`export_ready`, and
  `export_failed` and `export_expired` from the tidy) show no origin.
- The download is a fetch saved as a blob (revoked a minute after the save starts), so the
  browser holds the whole archive in memory once.
- Tables are paged on `id` (keyset), so a file is in id order, not time order, and the pages are
  not one snapshot: a row written during the build may or may not be in it.

## The doctor PDF (issue 210, ADR 0013 section 6)

A second `exports` kind, `doctor_pdf`, in the same tables (no migration): `reports/doctor_routes.py`
(`/api/reports/doctor`: `POST`, `GET`, `GET /{id}/file`) uses the export's step-up, rate limit,
one-build-per-owner, single-use download and chunk storage (`export_store` takes the kind;
`export_routes.serve_download` is the shared download). Audit kinds are `doctor_pdf_requested` and
`doctor_pdf_downloaded`. The worker job is `doctor_pdf_build` (`reports/doctor.py`).

| Module | Role |
|---|---|
| `doctor_input.py` | the request body and job input: period (at most 12 months), metric keys, at most 10 recording ids; `extra="forbid"` so no name or free text travels |
| `doctor_read.py` | the reads, all through the user's context: daily series, birth year, device-tier findings, ECG strips (via `ecg_facts.py`) |
| `doctor_model.py` | pure: the `Report`, monthly rows, the footer and range wording, device-finding text (E1 to E3) |
| `doctor_layout.py` / `doctor_pdf.py` | pure page layout and the reportlab build (`invariant=1`, deterministic bytes); ECG strips reuse `ecg_pdf.py` at 25 mm/s and 10 mm/mV |

What the PDF holds: header with period, generation date and birth year if set, a blank
"Name: ______" line; per metric a chart with the personal typical range ("from your own last 90
days; not a clinical reference range"), a monthly table (median, min, max, days, "entered
manually"; attrib 2 and 4 drawn hollow); Withings' device findings, attributed to Withings;
chosen ECG strips; a footer on every page. Never in it: estimates, experimental or trend findings,
scores, AI text, attrib 1 readings, hidden findings. Findings come only from `views.findings_read`.

Tests: `test_reports_doctor_pdf.py` (extracted text has no estimate or experimental finding,
strip geometry, determinism, input limits) and `test_reports_doctor_api.py` (step-up, RLS and 404
across users, single use, rate limit). Frontend: `components/reports/DoctorReport.test.tsx`.

Known gaps:

- The tidy's audited `export_failed` and `export_expired` for doctor rows carry `export_kind`
  "data" (the purge's carry the row's kind).
- No `export_ready` mail for a doctor PDF (it is requested and fetched in one sitting).
- The ECG picker offers the first page of recordings only (newest, with a signal).
- ECG pages keep their own "Page i of k".
- The purge's 90-day delete of `failed` and `expired` rows filters on state in its SQL only;
  no test pins that filter (a row in another state older than 90 days must stay).
- A month's min and max are the min and max of the daily minimums and maximums.
- A metric whose typical range cannot be computed prints "No typical range yet"; one with no data
  prints "No readings in this period."
- An ECG that is too long or has no signal gets a note on the findings page instead of a strip.
