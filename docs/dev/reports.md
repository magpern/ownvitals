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
| `frontend/src/components/reports/ExportCard.tsx`, `frontend/src/api/exports.ts` | "Export everything" in Settings → Your data |
| `e2e/specs/export.spec.ts` | step-up → export → download |

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
   byte (at most 3). The body streams from the chunks with `Content-Disposition: attachment`,
   `Cache-Control: no-store`, `Content-Type: application/zip`, `X-Content-Type-Options: nosniff`
   and `Content-Length`. After the last byte the row is `expired` with `download_count` 1 and the
   chunks are deleted. Audit `export_downloaded` (attempt, completed).
4. The SPA fetches the file (same-origin) and saves it as a blob; the shared step-up dialog
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
  carries the same `label`). Without the tier, `withheld` lists `ecg_estimates` and
  `experimental_findings` with `experimental_tier_not_enabled`.

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
| `test_reports_export_api.py` | the request without recent auth, without step-up, with both; one build at a time; 3 a day; the list per user; a download streams once (headers, size, SHA-256) and is then refused; no export id in the logs; the download without step-up, cross-site (`cross-site`, `same-site`, `none`, missing) without counting, from another user (404); 3 attempts; expiry deletes the chunks; old rows go after 90 days; a build that ended without an archive; the size limit |
| `test_reports_export_contents.py` | two users with a row in every owned table: A's archive holds none of B's ids, email or events, and the other way round; exactly the allowlisted columns in JSON and CSV; no planted secret; the experimental tier with and without the switch, hidden findings marked |
| `test_reports_export_allowlist.py` | every owned and account table and column is classified; no binary column exported |
| `test_reports_export_format.py` | the CSV-injection rule, numbers untouched, round trips, canonical JSON, the chunked zip, the size limit |
| `test_reports_export_db.py` | the vocabularies, forced RLS, one build per owner, chunks only of own exports, the cascade |

## Known gaps

- **The cross-user purge** (`exports_purge_expired()`, ADR 0013 §4: a `SECURITY DEFINER` function
  in `docker/postgres/exports/60-exports.sql`, pinned in `queue_digests.py` and guard 9, run
  hourly by the worker) is not built: those files are changed by the open webhook-hardening PR.
  Until it lands, an expired archive cannot be downloaded (the download checks `expires_at`), and
  its chunks are deleted the next time its owner lists, requests or downloads; an archive of an
  owner who never comes back stays in `export_chunks` (and in backups) until then.
- **FIT** (`export_fit`, ADR 0013 §3) is off by default and not built.
- **The `export_ready` mail** (ADR 0007's outbox) is not sent; the Settings row polls instead.
- The export kinds are not yet in the activity list's allowlist (`activity_model.SHOWN`).
- The download is a fetch saved as a blob, so the browser holds the whole archive in memory once.
- Tables are paged on `id` (keyset), so a file is in id order, not time order, and the pages are
  not one snapshot: a row written during the build may or may not be in it.
