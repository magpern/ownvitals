# ADR 0013: Reports and export

Status: Proposed (#204). Security-critical (PLAN-operations §10.1: export). Open questions for
the owner are listed at the end with the default that applies until they are answered. Its
companion for M7's AI part is [ADR 0014](0014-ai-insights.md).

## Context

PLAN §11 M7: "doctor PDF, JSON/CSV export with step-up (FIT where the data supports it)". PLAN §6:
"Export requires step-up when it lands (M7)", and ADR 0003 §4's table leaves the export row's
recent-authentication cell to "M7 ADR". ADR 0006 fixes what every output of ECG data must do: the
JSON/CSV export carries **every finding with its overlay state** (the user's complete data) through
the one read function (§11), **only up-to-date estimates** in a section of their own labelled
"Estimated by ownvitals …" (§9), and the doctor PDF and AI context leave hidden findings out; the
ECG PDF shows no ownvitals estimates and nothing experimental, says the classification is
Withings' and that ownvitals is not a medical device (§3). D9 keeps experimental output behind the
owner-only flag on household accounts. The threat model's placeholder T74 ("a stolen session
exporting a user's full history") waits for this ADR. The Settings mock-up reads "Export
everything: JSON and CSV, every reading and finding with its provenance. Needs your authenticator
code", and "Experimental results … never go into the doctor PDF".

Withings itself gates `DataExport` behind its paywall (findings §3); here export is free (D11)
and is also the user's way to take their data elsewhere.

## Decision

### 1. What the export holds

One archive per request, `ownvitals-export-<date>.zip`, with:

- `manifest.json`: `format` `ownvitals-export/1`, the app version, the generation time, the
  account's id, the list of files with row counts and SHA-256 each, and the reasons for anything
  withheld (§2).
- **JSON** (`data/*.json`, one file per table, arrays of objects, UTF-8, canonical key order) and
  **CSV** (`csv/*.csv`, the same rows flattened; nested JSON columns as a JSON string in one cell),
  for every owned table that holds the user's data:
  - account: email, profile (birth year, sex for score bands, day assignment, display settings);
  - connections: provider, status, scopes, created and disconnected times, the Withings user id;
  - devices, measure groups with their measures (raw value, unit and the real value) and raw
    payloads, observations, `metric_daily`, sleep summaries and series, activity days, workouts,
    intraday series;
  - ECG recordings: metadata, Withings' reported values (`reported`), and the signal as an array
    of integers in µV (JSON) or a long table `recording_id, index, microvolts` (CSV);
  - findings: **every** finding, every status and tier the account has, each with its overlay
    state from ADR 0006 §11's read function (`shown`, `hidden_gate_failed`, `not_evaluated`),
    `rule_id`, `rule_version`, evidence, confidence, `data_quality`, origin and provenance;
  - scores (ADR 0012) with components and provenance;
  - the user's own `auth_events` (kinds, times, IPs, places; the 30 days that exist);
  - AI consent records and, if not withdrawn, AI conversations and digests (ADR 0014).
- **Never** in the export: tokens, client or webhook secrets, any hash, HMAC or ciphertext
  (password, TOTP seed, recovery codes, session or pre-session tokens, passkey public keys and
  user handles), job rows and events, rate-limit buckets, system tables (`webhook_events`,
  `withings_apps`, the admin audit log), other users' anything. The builder works from an
  **allowlist of tables and columns** in code; a column not on it is not exported, and a guard
  test fails when an owned table or column is neither on the allowlist nor on an explicit
  "not exported" list with a reason.
- **CSV injection**: a text cell that begins with `=`, `+`, `-`, `@`, a tab or a carriage return
  is prefixed with `'` (OWASP's CSV-injection rule); numeric columns are written as numbers and
  never prefixed. Text from Withings (device names, time-zone strings) is user-adjacent input.

### 2. Estimates and experimental items in the export

- **Up-to-date estimates only** (ADR 0006 §9): read through the one read function; stale ones are
  left out, never marked. They go in `data/ecg_estimates.json` and `csv/ecg_estimates.csv`, whose
  header and manifest entry say "Estimated by ownvitals from the raw signal; not a Withings
  measurement", never in a Withings column.
- **Visibility follows the screen.** Estimates and experimental findings are shown only behind the
  owner-only flag (D9, ADR 0006 §9, ADR 0010 §7), so the export holds them only for an account that
  sees them in the app. For any other account they are withheld and `manifest.json` says so
  (`withheld: ["ecg_estimates", "experimental_findings"]`, reason `experimental_tier_not_enabled`).
  Experimental findings are not computed for such accounts anyway (ADR 0010 §7); estimates are.
  Whether every user should receive their estimates in the export is the owner's (open question 3).
- The rest (Withings data, trend and device findings, scores the account can see) is always in.

### 3. FIT

- **Off by default** (setting `export_fit`, default false; open question 2). When on, the archive
  adds `fit/body-composition.fit`: one FIT `weight_scale` message per measure group with weight,
  fat percentage, muscle mass, bone mass and hydration where Withings reported them (the streams
  `withings-sync` uploads, findings §4), timestamps in UTC. Workouts and intraday streams are not
  in FIT v1: Withings workouts are summaries, and FIT activity files built from them would invent
  records.
- **Our own encoder** in `ownvitals.export.fit` (the FIT file format is documented by Garmin; we
  write only the header, definition and data messages we need, and the CRC), so no FIT SDK is
  added (Garmin's SDK licence is not on an accepted list). Tests decode the output with our own
  decoder and check the CRC and every field against the rows.
- No estimate and nothing experimental ever goes into FIT.

### 4. Requesting, building and downloading

- **Request** `POST /api/exports` behind **`require_sensitive`**: recent authentication and
  step-up (both within 10 minutes; ADR 0003 §4's export row: recent auth **yes**, step-up yes).
  Why both: the export is the whole history in one file, at least as sensitive as deleting it,
  which needs both. One export in progress per account; at most 3 requests per 24 hours
  (fixed window, HMAC-ed bucket key, 429 with `Retry-After`).
- **Build** as a job, `export_build` (user lane, origin `user`, idempotency key), in the worker,
  in the requesting user's `UserContext` (ADR 0002, ADR 0005 §4): every read is under forced RLS,
  so the builder can't reach another user's rows even by a bug. It streams the archive into
  `export_chunks` (an owned table: `owner_id`, `export_id`, `seq`, `bytes` of at most 4 MiB),
  fenced like every worker write. Progress goes over the job's SSE events (counts only, T47).
- **Size limit**: the archive may reach `export_max_bytes` (setting, default **1 GiB**). Over it, the
  job fails with `export_too_large`, deletes its chunks, and the UI says which part was largest
  (ECG signals or intraday, usually); a later option to exclude intraday is a UI change, not a
  security one.
- **The export row** (`exports`, owned): `id` (UUID), `kind` (`data`, `doctor_pdf`), state
  (`building`, `ready`, `failed`, `expired`), `created_at`, `ready_at`, `expires_at` (ready +
  **24 h**, setting), `size`, `sha256`, `download_count`.
- **Download** `GET /api/exports/{id}/file`, authorised by the **session cookie and a step-up
  within 10 minutes** (`require_stepup`) at download time, under RLS (404 for another user's id).
  There is **no URL secret**: the path holds only the export's id, which grants nothing on its
  own, so no signed URL can land in a log, a `Referer` or browser history. `Content-Disposition:
  attachment`, `Cache-Control: no-store`, `Content-Type: application/zip` (or `application/pdf`),
  `X-Content-Type-Options: nosniff`. The body is streamed from the chunks.
- **Single use, short-lived.** A download that streams to the end sets `download_count` and
  deletes the chunks at once (the row stays, `expired`, as a record); an interrupted download can
  be retried, at most 3 attempts in all, until `expires_at`. The hourly purge deletes the chunks
  of expired exports. "Single-use" therefore means one completed transfer.
- **Not under the envelope.** Chunks hold the same data as the source rows, under the same RLS
  and backups (health data at rest is not enveloped; accepted risk 2). An export that exists at
  the nightly dump is in that backup; it ages out with the backups (accepted risk 13).
- **Notification**: in-app when ready (the job's events); mail through ADR 0007's outbox only as a
  fixed kind `export_ready` that says something is waiting, with the Settings link (T59). Never
  a download link in a mail.

### 5. Audit

`auth_events` kinds: `export_requested`, `export_ready`, `export_failed` (with a reason code),
`export_downloaded` (with the attempt number and whether it completed), `export_expired`;
`doctor_pdf_requested` and `doctor_pdf_downloaded` likewise. They hold the export id, kind, size
and IP, never content. Every one is listed in the user's "Recent security activity".

### 6. Doctor PDF

A report for a clinician, built by the same job machinery (`kind` `doctor_pdf`, request behind
`require_stepup`, download as §4; **step-up yes, recent auth no**: it holds a chosen subset, not
the whole history).

- **The user chooses**: a period (up to 12 months), the metrics (from those with data), and
  which ECG recordings (at most 10 strips).
- **Contents**, in this order:
  1. a header: "Health data report", the period, the generation date, the user's birth year if
     set (no name or email unless the user types a name for this report; it is not stored);
  2. per chosen metric: a chart of daily values with the **personal typical range** (ADR 0010 §2's
     display band, labelled "Your typical range, from your own last 90 days; not a clinical
     reference range"), and a table of
     monthly median, minimum, maximum and number of days; units as Withings reported them;
  3. **device-tier findings** in the period (E1 to E3: "Withings classified this ECG as …"),
     attributed to Withings, with their dates; hidden findings left out (ADR 0006 §11);
  4. the chosen **ECG strips at true scale** (ADR 0006 §3's PDF layout: 25 mm/s, 10 mm/mV,
     calibration pulse, local time and zone, device model, sampling rate, lead label, Withings'
     values with attribution);
  5. a footer on every page: "Data recorded by Withings devices and retrieved through the
     Withings public API. Classifications are Withings'. ownvitals is not a medical device and
     this report is not a diagnosis."
- **Never in the PDF**: ownvitals estimates, experimental findings, trend-tier findings, scores
  (the HIS is experimental by default and Body Path is a projection, ADR 0012), AI text, and
  `attrib=1` (unconfirmed) readings. There is **no owner-flag variant** in v1 (open question 1).
  Why trend findings stay out: their text is our description of the user's history; the clinician
  gets the data and the range instead.
- The renderer is the M5 ECG PDF's (ADR 0006 §3), so the strips print at 100 % at true scale.
- Fonts are embedded from the app's own files; the PDF contains no external reference, script,
  form or attachment.

### 7. Tests that prove it

| Decision | Test |
|---|---|
| §1 contents and the allowlist | `test_export_contents.py`: a synthetic account with a row in every owned table; each allowlisted column present, each forbidden one absent (every secret kind planted and grepped across all files of the archive); the guard over all owned tables and columns |
| §1 CSV injection | `test_export_csv.py`: each leading character prefixed, numbers untouched, a round-trip of every table |
| §2 estimates and experimental | `test_export_tiers.py`: a stale estimate absent; a current one only in its own file with the label; a flagged account gets estimates and experimental findings, an unflagged one gets neither and the manifest's `withheld`; every finding present with its overlay state, hidden ones included |
| §3 FIT | `test_export_fit.py`: off by default; on: decode, CRC, every field equals the rows; no estimate field |
| §4 request | `test_auth_stepup.py` extended (export: refused without recent auth, without step-up; accepted with both); `test_exports_api.py` (one in progress, the 24 h limit) |
| §4 isolation | `test_export_isolation.py`: two synthetic users; user A's export holds none of B's rows; B's session gets 404 for A's export id; the job runs only in A's context (RLS tests in the worker context) |
| §4 download | `test_exports_download.py`: refused without step-up; 404 cross-user; headers; a completed download deletes the chunks; three attempts then refused; expiry purges; no export id or token-like value in logs (the logging test) |
| §4 size | `test_export_size.py`: a limit set low fails the job with `export_too_large` and leaves no chunk |
| §5 audit | `test_exports_api.py`: each event written, none with content |
| §6 doctor PDF | `test_doctor_pdf.py`: text extraction finds each section, the footer on every page; no estimate, experimental or trend finding, score or `attrib=1` value appears even when the account has them; hidden findings absent; the ECG strip's scale (mm per second and per mV from the PDF's drawing units) within 0.5 % |
| e2e | `export.spec.ts`: request with step-up, wait for ready, download, unzip, manifest checks |

## Alternatives

- **A signed, time-limited URL** for the download (the classbeat pattern for files). It is a bearer
  secret in a URL, which ends up in proxy logs and history; the session cookie plus step-up does
  the same job with nothing to leak. Rejected.
- **Build in web, in the request.** A large history would hold a web worker and a database
  connection for minutes; the worker has the job machinery, fencing and lanes. Rejected.
- **Store the archive on a shared volume.** Web and worker share only the database; a volume
  would add a path outside RLS. Rejected.
- **Envelope-encrypt the chunks.** Adds key handling for data that sits next to the same values
  unencrypted in its source tables. Rejected (§4); revisit with accepted risk 2 at M8.
- **Step-up only for the export** (no recent auth). Cheaper by one prompt; the whole-history copy
  justifies both proofs, as for delete. Rejected.
- **Trend findings in the doctor PDF.** Possible later as an owner choice; out by default (§6).
- **FIT activity files from workouts.** No per-second records in a workout summary. Rejected.

## Consequences

- The M7 export issue adds `exports`, `export_chunks`, the `export_build` job kind, the allowlist,
  the routes and `export.spec.ts`; the M7 doctor-PDF issue adds the report builder on the M5
  renderer. Both are security-critical (Opus).
- ADR 0003 §4's table row "M7: export" becomes: recent auth **yes**, step-up **yes**; a row "M7:
  doctor PDF": recent auth —, step-up **yes**; download of either: step-up **yes**.
- ADR 0007 gains the outbox kind `export_ready` (no link to the file, the Settings route only).
- `docs/dev/export.md` is written by the export issue.
- Threat model: section K's T90 to T95; T74 points there.

## Open questions

**For the owner** (each has a default that applies meanwhile; the Architect records them in
`docs/status.md` "Blocked on the owner"):

1. **Estimated values or experimental items in the doctor PDF**, as an owner-flag variant?
   Default meanwhile: **none**; no variant exists. Safe-off: none.
2. **FIT: which streams?** Default meanwhile: **off** (`export_fit` false); when turned on, body
   composition only (§3). Safe-off: off.
3. **ECG estimates for accounts without the experimental flag** in their own export (a data-access
   argument for, D9 against). Default meanwhile: **withheld** and said so in the manifest (§2).
   Safe-off: withheld.

**Decided by the Architect (2026-11-03, this ADR).** Contents and the allowlist; CSV-injection
rule; recent auth and step-up for the export, step-up for the PDF and for every download; the
worker job, chunks in an owned table, 1 GiB and 24 h; no URL secret; single completed download;
the PDF's contents and exclusions.
