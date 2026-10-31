# ownvitals: plan

**ownvitals** is a multi-user website that pulls each user's Withings data through the free
public API (webhooks plus scheduled sync), stores it, analyses it (trends, outliers, ECG and
nerve-health signals, health scores) and shows it on a dashboard. The backend, and especially the
Withings integration, must be production-grade from the start. The UI starts as an MVP and gets
redesigned later.

- Repository: `magpern/ownvitals` (public).
- Host: `withings.pernemark.se`.
- Evidence from reverse-engineering the Withings app: [withings-findings.md](withings-findings.md).

**Revision 2 (2026-10-31)** incorporates an external architecture review:
- corrected webhook-delivery wording
- crash-safe token rotation
- mandatory 2FA moved to M0, before any real data
- webhook hardening
- capability discovery
- tiered findings with a medical-claims gate
- provenance and minimum-data rules
- a scoped deploy mechanism
- an earlier legal gate
- integration-status levels

The agent merge gate (§10.2) is deliberately unchanged and stays enforced by the agents, not by
GitHub.

---

## 1. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | **Public Withings API only** (`wbsapi.withings.net`, OAuth2), never the app's private `scalews` API | The private API needs Withings passwords, likely breaks the terms, and changes without notice |
| D2 | **One platform Withings app**; users click "Connect Withings". BYO client credentials and the 5,000-user free cap are **assumptions to verify** against Withings' terms before M8 | No developer setup for users. BYO isn't an approved way around quotas until Withings confirms it |
| D3 | **Webhooks are the primary trigger, backed by scheduled polling and reconciliation.** Withings retries failed deliveries for about 5 h, but not indefinitely, and deletions aren't announced. **Sync cursors, not webhook delivery, define correctness** | Keeps syncing correct under missed, late or duplicated notifications |
| D4 | **Raw payloads plus normalized observations with provenance**; unknown meastypes kept | New types keep appearing; re-processing without re-fetching |
| D5 | **Compute our own scores and insights** | Rebuilds the Withings+ features without the subscription (findings §3) |
| D6 | **Stack = classbeat's**: Python 3.12, FastAPI, SQLAlchemy 2 async, psycopg 3, Alembic, pydantic v2, httpx, uv; React 19 + Vite + TS SPA; **PostgreSQL 17**. No Redis, Kafka, Kubernetes, TimescaleDB or microservices | Reuses classbeat's tested platform layer (§3). Timescale continuous aggregates don't support RLS |
| D7 | **Audience: private first, public later**, with a **legal and security gate before any non-household user** (M8) | The household exemption depends on what the processing is and who it serves, not on where the server is |
| D8 | **Host: home server now, EU VPS before going beyond the household** | Acceptable for private use only (§9) |
| D9 | **Three finding tiers** (§7.2): device-reported (Withings' own results, attributed), descriptive trends (comparison with your own history), experimental interpretation (owner-only flag, never shown to non-household users until a medical-claims review) | Disclaimers alone don't avoid medical-device classification; intended purpose does |
| D10 | **Agent-built, gate-reviewed** (classbeat's AGENTS.md), with **status levels** IMPLEMENTED → INTEGRATION_VERIFIED → PRODUCTION_READY | Green CI isn't the same as a verified provider integration |

---

## 2. Architecture

```
 Browser ──HTTPS──▶ Cloudflare ──▶ SWAG nginx (192.168.1.151, *.pernemark.se cert)
                                         │  withings.pernemark.se
                                         ▼
                     192.168.1.222:8096  ┌──────────────────────────────┐
                                         │ web (FastAPI + built SPA)    │◀── Withings webhook
                                         │ /api/*, /api/webhooks/...    │    POST/HEAD
                                         └───────┬──────────────────────┘
                                                 │ jobs table + LISTEN/NOTIFY
                                         ┌───────▼──────────────────────┐   rate-limited
                                         │ worker (same image)          │──▶ wbsapi.withings.net
                                         │ sync, ECG fetch, analysis,   │──▶ api.anthropic.com (opt-in)
                                         │ scheduler, purge             │   (egress allowlist)
                                         └───────┬──────────────────────┘
                                         ┌───────▼──────────────────────┐
                                         │ PostgreSQL 17, RLS forced,   │
                                         │ owner_id on every owned table│
                                         └──────────────────────────────┘
```

- **One image, two processes** (web, worker), as in classbeat ADR 0001. Same origin, so no CORS.
- **The worker owns every outbound call.** The outbound allowlist (`egress.py`) permits only
  `wbsapi.withings.net`, `account.withings.com` and, when AI is enabled, `api.anthropic.com`.

### Repository layout (mirrors classbeat ADR 0001)
```
backend/            uv project, package ownvitals (src/ownvitals), tests/, migrations/ (Alembic)
  src/ownvitals/    auth/ crypto/ connections/ db/ jobs/ worker/ admin/ flags/     ← ported from classbeat
                    withings/ (client, oauth, tokens, sync, webhooks, rate, capabilities)  ← new
                    ingest/ analysis/ ecg/ scores/ findings/ reports/ insights/    ← new
frontend/           React 19 + Vite + TS SPA (api client, session, guards ported)
e2e/                Playwright + fake servers: fake-oidc (ported), fake-withings (new)
docker/  scripts/   postgres init (roles), queue SQL; deploy script (§9.2)
docs/               roadmap.md (this plan, frozen per revision), status.md, adr/, security/, runbooks/
.claude/agents/     developer.md, reviewer.md, explorer.md
```

---

## 3. What we reuse from classbeat

Port the files at a pinned classbeat commit, recorded in ADR 0001, and rename the package. It's
a copy, not a shared library.

**Every ported component gets a threat-model review for health data in M0.** Being secure enough
for classbeat doesn't make it secure enough here.

| classbeat | Use in ownvitals | Change needed |
|---|---|---|
| `auth/*` (passwords, sessions, cookies, TOTP, Google OIDC, rate limits, email flows, devices, purge), ADR 0003 | Accounts and login | **Mandatory TOTP and step-up in M0**; passkeys and QR login in M3 |
| `crypto/envelope.py`, `keks.py`, `keys_cli.py`, `connections/rewrap.py`, ADR 0004 | Token vault (AES-256-GCM, AAD `user_id\|provider`, KEK rotation) | Provider `withings`; webhook-secret pepper in the KEK ring |
| `connections/*` (Spotify OAuth, refresh, status) | Withings connection | Token generations and refresh recovery (§4.3) |
| `context.py`, `db/rls.py`, `role_check.py`, ADR 0002 + AST guards | Isolation of every health table | Issuer for webhook- and scheduler-triggered jobs; RLS tests in every execution context |
| `jobs/*` + `worker/*`, ADR 0005 | Backfill, sync, ECG, analysis | System and webhook kinds, priorities, singleton keys, rate-limit hold, dead-letter |
| `admin/audit*`, `flags/*`, `log.py`, `headers.py`, `auth/clientip.py` | Audit, flags (AI, experimental tier), JSON logs, headers, real IP | Trusted proxies = SWAG + Cloudflare; log redaction of webhook paths |
| `egress.py`, `agent/providers/anthropic.py`, `evals/*` | Allowlist; AI insights and evals (M7) | New hosts, tools and cases |
| `e2e/fake-oidc`, CI, commitlint, dependabot, CODEOWNERS, PR template, rulesets, release | Same pipeline | Rename; add `fake-withings` and the analysis tests |
| `AGENTS.md`, `CLAUDE.md`, `.claude/agents/*`, ADR/status/runbook/threat-model formats | Agentic workflow (§10) | Health-data rules, status levels |
| `plans/`, `quota/`, `songs/`, `spotify/`, `design/`, `playlists/` | Not ported | — |

---

## 4. Withings integration (M1)

### 4.1 Setup
- **Two Withings developer apps.**
  - `ownvitals-dev`: local redirect; restricted mode (10 users) is fine.
  - `ownvitals`: redirect `https://withings.pernemark.se/api/oauth/withings/callback`;
    notifications to `/api/webhooks/withings/<secret>`.
- **The base URL is set per Withings app**, not per connection. The standard public integration
  uses `wbsapi.withings.net`; the US Medical Cloud is a separate contracted environment.
- Tests use `fake-withings` and the demo account only. **Tests never touch real accounts.**

### 4.2 Client and capabilities
- Async httpx, `POST` form-encoded with a Bearer token. **Check JSON `status`**, because errors
  come back as HTTP 200.
- Errors:
  - `TokenInvalid` (401/2553/2555): go through §4.3, never a generic retry.
  - `RateLimited` (601): hold the app bucket.
  - `Transient` (HTTP 408/429/5xx, timeouts): backoff with jitter, within the job's retry budget.
  - `Permanent`: a categorized failure; provider text is never stored.
- pydantic models per response; unknown fields kept in `raw`. **Tests cover undocumented enum
  values, missing optional fields and partial records.**
- **Capability discovery**:
  - Each (connection, stream) has a capability: `supported | unsupported | not_authorized |
    not_available | unknown`, learned from scopes, devices, responses and the probe (§10.4).
  - **Missing data is never read as zero or normal.**
  - Backfill scope is limited by what the provider actually paginates and retains.
- Endpoints: `getmeas`, `heart list/get`, `getactivity`, `getintradayactivity`, `getworkouts`,
  `sleep get/getsummary`, `user getdevice/getgoals`, `notify subscribe/list/update/revoke`,
  `oauth2 requesttoken/revoke`. Hand-written client (the OpenAPI file can't be fed to generators).

### 4.3 OAuth and token rotation (crash-safe)
- `start`: `state` bound to the session (10-min TTL, single use).
- `callback`: **exchange the code in the request (it's valid for 30 s)**, then upsert by
  `(app_id, withings_userid)`. Refuse it if that Withings user is linked to another account.
- Tokens are envelope-encrypted (ADR 0004) and carry a **`token_generation`** number. All
  updates are **compare-and-swap** on the generation, so an older generation can never overwrite
  a newer one.

Withings facts this relies on (findings §2):
- The access token lives 3 h; the refresh token lives 1 year.
- After a renewal, **the old refresh token stays valid for up to 8 h or until the new access
  token is first used**.

Refresh protocol:
1. Take the per-connection advisory lock. Write a durable intent row
   (`refresh_in_flight = generation`) and commit.
2. Call `requesttoken` with the current refresh token.
3. On success: in one transaction, CAS-write the new tokens as `generation + 1`, clear the
   intent, commit. **Only then use the new access token** (using it first would invalidate the
   old refresh token before it's saved).
4. If the response is ambiguous (timeout, crash, or a lost response before commit): a recovery
   job sees the stale intent and retries **once** with the stored refresh token. That's safe
   because the new access token was never used and the 8 h window still applies. If that fails,
   or the intent is older than 8 h, set the connection to `needs_reconnect` and notify the owner.
5. Token refresh **never goes through the generic HTTP retry path.**

Tests:
- crash immediately after receiving the refresh response
- crash after commit but before first use
- two workers racing
- stale intent older than 8 h
- CAS rejecting an older generation

Disconnect: `notify revoke` for each appli, then `oauth2 revoke`, then delete secrets. Account
delete does the same before the purge.

### 4.4 Sync engine
| Stream | Backfill | Incremental | Webhook appli |
|---|---|---|---|
| `measures` | full history, 1-year windows | `getmeas lastupdate` (category 1 and 2) | 1, 2, 4, 58, 62, 63 |
| `ecg` | full, 3-month windows of `heart list`, then `heart get` per signal | list since cursor | 54, 55 |
| `activity_daily`, `workouts` | full history | `lastupdate` | 16 |
| `sleep_summary` | full history | `lastupdate` | 44 |
| `sleep_series` | last 90 days, more on demand | per night | 44 |
| `intraday` | last 30 days, more on demand | rolling | 16 |
| `devices`, `goals` | on connect | daily | 46 |

Job kinds:
- `withings_connect`, `withings_backfill` (resumable, lowest priority), `withings_sync`
  (singleton per connection and stream, 30 s debounce), `ecg_fetch`, `withings_reconcile`
  (nightly), `withings_poll` (6-hourly safety net), `token_refresh_recover`.
- Every job has a **retry budget**, a terminal failure category and a **dead-letter** state.
- Operators replay jobs with `ownvitals jobs replay <id>` (audited).

**Per-stream completeness**: `sync_cursors` records the backfill range covered, the last full
page set and capability. The UI shows incomplete imports instead of implying "no data".

**Reconciliation and deletions**: a row is marked `deleted_at` only when **both**:
- the re-read window completed through every page with no errors, and
- the row is missing from two consecutive complete reconciliations.

Also: `notify list`, then re-subscribe or `notify update` on drift.

**Rate limiter**:
- Per Withings app: a Postgres token bucket, budget 100/min of the documented 120.
- Reservations carry a lease expiry, so a crashed worker's tokens come back. Several workers
  claim with `FOR UPDATE SKIP LOCKED`.
- A global concurrency cap (e.g. 4 in-flight calls).
- **Per-user fairness**: round-robin, with each user's backfill share capped.
- Lanes, highest first: user "sync now" > webhook > poll > reconcile > backfill.
- Status 601 pauses the bucket for `wait_seconds` or 60 s.

**Webhook receiver** (`/api/webhooks/withings/{secret}`):
- **Secrets**: high-entropy (256-bit) per app, stored only as **HMAC-SHA256(pepper, secret)**
  with a `secret_id`, compared in constant time.
- **Rotation**: two secrets are active at once; the worker re-points subscriptions with
  `notify update`, then the old secret is retired.
- **Request limits**: `HEAD` → 200. `POST` requires `application/x-www-form-urlencoded`, a body of
  at most 2 KB, and strict field validation (userid, appli, start/end dates within sane bounds).
- **Durable before 200**: insert into `webhook_events` (dedupe key: app, userid, appli,
  startdate, enddate) and the job in **one committed transaction**, then answer 200.
- **Replay and abuse resistance**: duplicates collapse into the singleton job; per-connection and
  per-app quotas on webhook-triggered jobs. The payload is a hint: data is always fetched from
  the API.
- **Unknown user**: a generic 200, with rate-limited internal logging.
- **No secrets in logs**: the path is redacted in app logs, traces and errors. SWAG uses a
  location with its own `access_log` format without the URI.

**Observability** (Prometheus, already on the home server):
- webhook delivery age (received − enddate)
- last successful sync per stream
- subscription health
- reconciliation drift (rows changed or deleted)
- queue age and dead-letter count
- refresh failures and recoveries
- API quota use and 601 count
- per-stream completeness

**M1 definition of done (IMPLEMENTED level)**
- [ ] Full flow against `fake-withings` and the demo account: connect, backfill with SSE
      progress, webhook-driven sync, poll, reconcile, disconnect and delete.
- [ ] Failure injection green:
  - token-rotation crashes (§4.3)
  - 601 storms
  - duplicate, out-of-order and forged webhooks
  - oversized and malformed bodies
  - worker crash mid-backfill (lease expiry → resume)
  - abandoned rate reservations
- [ ] Capability states drive behavior; missing streams are never shown as zero.
- [ ] RLS guard tests on every new table in web, worker, scheduler, webhook and admin contexts.
- [ ] Metrics exported for every item in the list above.

INTEGRATION_VERIFIED comes later: on the home server, the owner connects a real account and runs
the probe (§10.4). The open questions (findings §5) are answered and recorded in an ADR.

---

## 5. Data model

Ownership rules (ADR 0002, made explicit):
- Every owned table has `owner_id`, `UNIQUE (owner_id, id)` and **owner-inclusive composite
  foreign keys** (e.g. `(owner_id, connection_id) → connections(owner_id, id)`), so a child row
  can never point at another user's connection.
- Request paths use the user role with forced RLS.
- Workers act through a `UserContext` issued from the job row (also RLS).
- Cross-user work (queue claim, reaper, webhook ingest, rate bucket) goes **only** through narrow
  `SECURITY DEFINER` functions.
- No `BYPASSRLS` role in any ordinary path.

```
withings_apps        id, client_id, client_secret (enveloped), base_url, webhook_secrets
                     [{secret_id, hmac, active_from, retire_at}]                  (system, admin-only)
connections          classbeat table; provider 'withings'; public {app_id, withings_userid, scopes};
                     token_generation, refresh_in_flight, refresh_intent_at
                     UNIQUE (app_id, withings_userid) via generated column
sync_cursors         owner_id, connection_id, stream, capability, lastupdate, covered_from/to,
                     backfill_state jsonb, last_ok_at, last_error_category
webhook_events       app_id, withings_userid, appli, startdate, enddate, dedupe_key UNIQUE,
                     received_at, job_id                    (system; insert via SECURITY DEFINER only)
devices              owner_id, connection_id, hash_deviceid, model_id, type, fw, battery, last_session_at
measure_groups       owner_id, connection_id, grpid, measured_at (UTC), source_tz, attrib, category,
                     model_id, hash_deviceid, created, modified, deleted_at, raw jsonb, payload_hash
measures             owner_id, grpid, type, value_raw bigint, unit int, position int NULL
ecg_recordings       owner_id, connection_id, signalid, recorded_at, source_tz, model_id, afib,
                     afib_details, heart_rate, sampling_hz, wearposition, signal bytea (int32 µV, zstd),
                     reported jsonb (Withings values), estimated jsonb (ours, with algo version)
sleep_summaries, activity_daily, workouts; sleep_series, intraday (monthly partitions)
observations         owner_id, metric_key, ts, source_ts, source_tz, value, source_value_raw,
                     source_unit, conversion, quality_flags, source_ref   (monthly partitions)
metric_daily         owner_id, metric_key, day, morning, mean, min, max, n, algo_version
findings             owner_id, rule_id, rule_version, tier (device|trend|experimental), severity,
                     confidence, data_quality jsonb, origin (live|backfill), metric_keys[], window,
                     evidence jsonb, input_version, status, first_seen, last_seen
scores               owner_id, kind, day, value, components jsonb, algo_version, input_version
```
- **Provenance**: every derived row stores `algo_version` and `input_version` (the max `modified`
  of its inputs), so results can be reproduced and recomputed when an algorithm changes.
- **Partitions**: monthly partitions are created 3 months ahead by the scheduler; retention is
  per table (raw kept, intraday configurable); BRIN on `ts`. Restore drills include partitioned
  tables.

---

## 6. Identity and security

**M0** (before any real data):
- classbeat auth (password, Google OIDC, sessions, rate limits, notices, purge, audit)
- **TOTP 2FA mandatory** for every account
- protected sessions (rotation, idle and absolute limits, log out everywhere)
- **step-up 2FA** for connecting Withings

**M3**:

| Addition | Approach |
|---|---|
| Passkeys | WebAuthn. "Use a phone" via QR (hybrid transport) gives standard QR sign-in |
| QR approve-login | Desktop QR with a 60 s single-use nonce bound to the desktop session cookie; the signed-in phone shows the desktop's browser, OS, IP and city and asks to approve; desktop receives the session via SSE |
| Step-up extended | Export, delete account, 2FA changes |

Disconnect and delete (with upstream revoke and purge) are part of M1, because they must exist
before real users.

---

## 7. Analysis engine (worker jobs, Python)

### 7.1 Pipeline
1. **Normalize** raw rows into `observations`, keeping source timestamp, time zone, raw value and
   unit, and the conversion applied.
2. **Quality gates**:
   - exclude `attrib=1` (ambiguous household measure) and show it as "unconfirmed"
   - plausibility ranges
   - flag jumps over max(2 kg, 3 %) within 24 h
   - **Keep every distinct reading**: deliberate repeat measurements are legitimate. Only
     identical `grpid`s are duplicates.
3. **Daily value**: the first morning weigh-in (04:00–11:00 local), otherwise the median, with
   `n` and spread kept.
4. **Minimum-data policy per metric**: e.g. a trend needs ≥ 8 days with data spanning ≥ 60 % of
   the window, and a baseline needs ≥ 10 points over ≥ 21 days. Below that the result is
   "insufficient data", never a finding.
5. **Methods** suited to irregular, autocorrelated data:
   - robust baseline (rolling median and MAD over time windows, not point counts)
   - EWMA weighted by time gap
   - Theil–Sen + Mann–Kendall with autocorrelation correction (modified MK)
   - PELT change points only on series that pass the minimum-data policy
6. **Every finding** carries `confidence` and `data_quality` (n, coverage, gaps, measurement
   frequency).
7. **Backfill doesn't flood users**: findings computed from historical data get
   `origin=backfill` and go into a "history" view with no notifications. Only `live` findings
   whose window ends in the last 7 days notify.
8. **Scores**: the Health Improvement Score (findings §3 bands), Body Path trajectory with a
   prediction interval, and Readiness when sleep or watch data exists. Each one is versioned
   with provenance.

### 7.2 Finding tiers and initial rules
| Tier | What | Who sees it |
|---|---|---|
| **device** | Withings' own results, attributed ("Withings classified this ECG as…"), original meaning and status preserved | Everyone |
| **trend** | Descriptive comparison with the user's own history ("higher than your 90-day typical range"); no diagnostic labels | Everyone |
| **experimental** | Clinical thresholds, independent estimates and interpretations | Owner-only flag on household accounts; **never** for non-household users until a medical-claims review passes (M8) |

| Rule | Tier | Logic |
|---|---|---|
| E1 | device | Withings AFib result positive (`afib=1`, `afib_details` 1/11/12): highlighted, strip shown, "share with a doctor" |
| E2 | device | ≥ 3 inconclusive or noisy ECGs in the last 10 (`afib_details` 2/5) |
| E3 | device | `afib_details` 6/7 (low or high HR reported by Withings) |
| E4 | experimental | Clinical bands on Withings-reported intervals: QTc > 450/460 ms, > 500 ms; PR > 200 ms; QRS ≥ 120 ms |
| E5 | experimental | Our rhythm-irregularity index vs Withings' result |
| E6 | trend | Withings-reported QTc/QRS above the personal 90-day range |
| N1 | trend | "Insufficient data" until ≥ 4 measures over ≥ 4 weeks |
| N2/N3 | trend | Guided or repeated routine values below the personal range; significant 90/180-day decline; monthly summary |
| N4 | experimental | ESC (229) literature bands (< 40 / 40–60 µS) |
| N5 | trend | Left/right difference > 20 % when per-foot types exist |
| B1 | experimental | +2 kg in 3 d or +2.5 kg in 7 d with an extracellular-water rise (fluid-retention pattern) |
| B2, B4, B5, B6 | trend | Weight loss ≥ 5 %/6 mo; muscle −2 %/90 d; visceral fat or metabolic age rising; segmental asymmetry > 10 % |
| B3 | experimental | ECW/TBW > 0.40 |
| C1 | trend / experimental | Resting HR +10 bpm above the personal range for ≥ 3 days (trend); < 50 / > 100 (experimental) |
| C2 | trend | Vascular age − age above its score-card band, or rising |
| Q1 | system | No weigh-in for 14 days, low battery, connection needs reconnect, import incomplete |

### 7.3 ECG subsystem (own spec: ADR in M5)
The spec is written before any interpretation code.

1. **Verify from real data** (probe): units and amplitude calibration, lead identity,
   sampling frequency, any pre-filtering, and whether multiple arrays are simultaneous leads.
2. **Preserve and display first**:
   - the original signal stored unmodified
   - a viewer on a 25 mm/s × 10 mm/mV grid
   - a PDF
   - Withings-reported values shown as such
3. **Signal-quality gate**: a trace that fails the SQI produces no intervals, irregularity
   metrics or findings.
4. **Descriptive analysis**: HR and HRV (RMSSD, SDNN, pNN50) from validated R-peaks, stored in
   `estimated` with the algorithm version. **Estimated values are never shown as equivalent to
   Withings' certified measurements.**
5. **Six-lead derivation** (III = II − I, aVR = −(I+II)/2, aVL = I − II/2, aVF = II − I/2) only
   if step 1 proves the inputs are simultaneous, calibrated leads I and II.
6. **Interpretation** (ectopic beats, irregularity, interval estimates) stays experimental, with
   its own release gate.

### 7.4 AI insights (M7, opt-in, feature flag)
Weekly digest and a chat assistant on Claude, using classbeat's provider layer and evals.

- **Computed statistics are still personal health data** (pseudonymized, not anonymous). Treat
  them that way:
  - data minimization: the tools return only what the question needs
  - a provider data-handling and retention review before enabling
  - explicit consent; withdrawing it stops processing and deletes the conversation history
  - audit log with redacted content
  - prompt-injection tests in the eval suite
- Experimental-tier findings are never passed to the model for non-household users.

---

## 8. MVP UI (M6; replaceable)
React SPA from classbeat's skeleton; uPlot or ECharts; ECG on canvas.

Pages:
- Onboarding
- Dashboard
- Metric detail
- ECG list and viewer (+PDF)
- Nerve health
- Body composition
- Findings inbox (live / history; tier badges)
- Settings

Every view shows **data freshness, incomplete imports, unsupported or unavailable metrics, and
connection errors** explicitly.

---

## 9–10. Hosting, legal and agentic development

See [PLAN-operations.md](PLAN-operations.md): local development, the home server and the real-data gate (§9.1–9.2), legal gates (§9.3), agent roles, the merge gate, independent verification, status levels and the probe (§10).

---

## 11. Milestones

| Milestone | Scope | Done when |
|---|---|---|
| M0 Platform | Port classbeat (auth, crypto, connections, RLS, jobs, worker, admin, CI, agents, e2e); **mandatory TOTP, session protection, step-up for connect**; RLS tests in worker, scheduler, webhook and admin contexts; health-data threat-model review of the ported parts | CI green; e2e login + TOTP; guards pass; threat model updated |
| M1 Withings core | §4 complete (tokens, capabilities, webhook security, completeness, observability, disconnect/delete) | §4.4 checklist → **IMPLEMENTED** |
| M2 Home deploy (early) | §9.2: SWAG, Cloudflare, deploy user, backups + restore drill, rollback, monitoring. Then you connect your account and run the probe | Real-data gate passed; M1 **INTEGRATION_VERIFIED** |
| M3 Identity additions | Passkeys, QR approve-login, step-up for export/delete/2FA changes | Opus review PASS; e2e for each flow |
| M4 Analysis v1 | Normalization with provenance, daily values, minimum-data policy, baselines, trends, change points, trend-tier rules, Health Score, backfill/live separation | Rules tested on synthetic irregular and sparse series; reproducible on your data |
| M5 ECG & nerve health | ECG spec ADR, preserve + view + PDF, SQI gate, descriptive HRV, nerve trends; experimental rules behind the flag | Validated on your history (owner-run); experimental tier reviewed by you |
| M6 MVP UI | §8 | A household member completes onboarding unaided |
| M7 AI & reports | Digest, assistant, doctor PDF, JSON/CSV export (FIT where the data supports it) | Consent, provider review, privacy and prompt-injection evals green |
| M8 Beyond-household gate | §9.3 checklist, VPS move, human security review, Withings terms check (D2), load test of the rate limiter | Owner sign-off → **PRODUCTION_READY** for non-household users |

---

## 12. Risks
| Risk | Mitigation |
|---|---|
| Public API exposes one ECG lead, or not all fields per device | Capability discovery; probe; viewer supports 1..n leads |
| Nerve-health diagnosis is proprietary (`libsudo`) | Trend tier only; Withings' score shown as-is |
| Medical-device classification | Tiers (D9), experimental flag, medical-claims review in M8 |
| 120 req/min shared; cap and BYO unverified | Webhook-first, lanes, fairness, lazy intraday; verify terms (D2); Enterprise plan if needed |
| Missed webhooks or Cloudflare blocking them | Cursors are the source of truth; poll + reconcile; WAF rule; delivery-age alert |
| Token rotation crash or race | Generations + CAS, intent row, 8 h grace recovery, crash tests |
| Forged or replayed webhooks | HMAC-stored rotating secrets, size and type limits, dedupe, quotas, fetch-only payload |
| Misleading analytics on sparse data | Minimum-data policy, confidence, backfill/live separation |
| Shared home host compromise | Firewall, isolation, KEK off-DB, scoped deploy key, move before M8 |
| Correlated model errors in review | Deterministic security tests, human review at M8 |
| Divergence from classbeat | Pinned source SHA; security fixes ported deliberately |
