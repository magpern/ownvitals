# ownvitals: plan

**ownvitals** is a multi-user website that pulls each user's Withings data through the free
public API (webhooks plus scheduled sync), stores it, analyses it (trends, outliers, ECG and
nerve-health signals, health scores) and shows it on a dashboard. The backend, and especially the
Withings integration, must be production-grade from the start. The UI starts as an MVP and gets
redesigned later.

- Repository: `magpern/ownvitals` (public).
- Host: `withings.pernemark.se`.
- Evidence from reverse-engineering the Withings app: [withings-findings.md](withings-findings.md).

---

## 1. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | **Public Withings API only** (`wbsapi.withings.net`, OAuth2), never the app's private `scalews` API | The private API needs Withings passwords, likely breaks the terms, and changes without notice |
| D2 | **One platform Withings app**: users click "Connect Withings". Optional "bring your own client_id/secret" later | No developer setup for users. BYO gives a user their own 120 req/min budget and gets past the 5,000-user cap |
| D3 | **Webhooks first, 6-hourly poll, nightly reconciliation** | Notifications arrive in about 2 min but are never redelivered, and deletions aren't announced |
| D4 | **Raw payloads plus normalized observations**; unknown meastypes kept | New types keep appearing; re-processing without re-fetching |
| D5 | **Compute our own scores and insights** | Rebuilds the Withings+ features without the subscription (findings §3) |
| D6 | **Stack = classbeat's**: Python 3.12, FastAPI, SQLAlchemy 2 async, psycopg 3, Alembic, pydantic v2, httpx, uv; React 19 + Vite + TypeScript SPA; **PostgreSQL 17** (no Redis, no Timescale) | Reuses classbeat's tested platform layer (§3). Python also suits analysis and ECG. TimescaleDB continuous aggregates don't support row-level security, so we use plain partitioned tables |
| D7 | **Audience: private first, public later** | Launch for you and your family; build to public standards; legal review and host move before opening up (§9) |
| D8 | **Host: home server now, EU VPS before going public** | Fine for private use; public health-data processing is easier to defend on a dedicated host (§9) |
| D9 | **Wellness / informational framing**; Withings' certified results shown verbatim | Avoids medical-device claims (EU MDR) |
| D10 | **Agent-built, gate-reviewed**, using classbeat's AGENTS.md model | Proven loop with independent reviewers and a PASS-per-SHA merge gate (§10) |

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

- **One image, two processes** (web, worker), as in classbeat ADR 0001. Web serves `/api` plus
  the built SPA on the same origin, so there's no CORS.
- **The worker owns every outbound call.** Withings sync, ECG download, analysis and AI jobs run
  as job kinds with their own concurrency limits.
- **The outbound allowlist** (classbeat `egress.py`) permits only `wbsapi.withings.net`,
  `account.withings.com` and, when AI is enabled, `api.anthropic.com`.

### Repository layout (mirrors classbeat ADR 0001)
```
backend/            uv project, package ownvitals (src/ownvitals), tests/, migrations/ (Alembic)
  src/ownvitals/    auth/ crypto/ connections/ db/ jobs/ worker/ admin/ flags/     ← ported from classbeat
                    withings/ (client, oauth, sync, webhooks, rate)                 ← new
                    ingest/ analysis/ ecg/ scores/ findings/ reports/ insights/    ← new
frontend/           React 19 + Vite + TS SPA (api client, session, guards ported)
e2e/                Playwright + fake servers: fake-oidc (ported), fake-withings (new)
docker/             postgres init (roles), queue SQL
docs/               roadmap.md (this plan, frozen per revision), status.md, adr/, security/, runbooks/
.claude/agents/     developer.md, reviewer.md, explorer.md
AGENTS.md CLAUDE.md compose.yaml compose.prod.yaml Dockerfile .github/
```

---

## 3. What we reuse from classbeat

Port the files at a pinned classbeat commit, recorded in ADR 0001, rename the package to
`ownvitals`, and keep tests alongside. It's a copy, not a shared library; later fixes are ported
by hand.

| classbeat | Use in ownvitals | Change needed |
|---|---|---|
| `auth/*` (accounts, passwords, sessions, cookies, TOTP, OIDC/Google, rate limits, email flows, devices, purge) + ADR 0003 | Accounts, Google login, 2FA | Add **passkeys** (WebAuthn) and **QR approve-login** (§6); require 2FA before connecting Withings |
| `crypto/envelope.py`, `keks.py`, `keys_cli.py`, `connections/rewrap.py` + ADR 0004 | Withings token vault (AES-256-GCM, AAD `user_id\|provider`, KEK rotation) | None. Provider list gets `withings` |
| `connections/*` (Spotify OAuth flow, token refresh, status, errors) | Withings OAuth connection | New provider module; handle **refresh-token rotation** (§4.3) |
| `context.py` (UserContext), `db/rls.py`, `role_check.py` + ADR 0002 + AST guard tests | Isolation of every health table | Add issuer for webhook-triggered jobs (via the job row) |
| `jobs/*` (queue, leases, fencing, runner, progress, SSE events) + `worker/*` + ADR 0005 | Backfill, sync, ECG fetch, analysis, digests | Add system-scheduled and webhook-triggered kinds, priorities, singleton (debounce) keys, rate-limit hold |
| `admin/audit*`, `flags/*`, `log.py`, `headers.py`, `auth/clientip.py` | Audit log, feature flags (AI opt-in), JSON logs, security headers, real client IP behind Cloudflare→SWAG | Trusted proxies = SWAG + Cloudflare ranges |
| `egress.py` + test scan | Outbound allowlist | New host list |
| `agent/providers/anthropic.py`, `evals/*` | AI insights (phase 6) and their eval suite | New tools and cases |
| `e2e/fake-oidc`, Playwright helpers | Login e2e | Add `fake-withings` (OAuth, data endpoints, webhook sender) |
| CI (`ci.yml`, `pr-title.yml`, `release.yml`), commitlint, dependabot, CODEOWNERS, PR template, rulesets | Same pipeline | Rename; add the analysis test suite |
| `AGENTS.md`, `CLAUDE.md`, `.claude/agents/*`, ADR/status/runbook/threat-model formats | Agentic workflow (§10) | Withings and health-data specifics |
| `plans/`, `quota/`, `songs/`, `spotify/`, `design/`, `playlists/` | Not ported | (quota may return for the public phase) |

---

## 4. Withings integration (milestone M1: must be fully working)

### 4.1 Setup
- **Two Withings developer apps.**
  - `ownvitals-dev`: redirect to the local dev server. Restricted mode (10 users) is fine.
  - `ownvitals`: redirect `https://withings.pernemark.se/api/oauth/withings/callback`,
    notifications to `https://withings.pernemark.se/api/webhooks/withings/<path-secret>`.
- Use the **demo account** endpoint for live smoke tests.
- `fake-withings` covers every other test. **Tests never touch real accounts.**

### 4.2 Client (`ownvitals.withings.client`)
- Async httpx; every call is `POST` form-encoded with a Bearer token. Parse JSON and **check
  `status`**, because errors come back as HTTP 200.
- Errors:
  - `TokenInvalid` (status 401/2553/2555): refresh once, then mark `needs_reconnect`.
  - `RateLimited` (601): hold the bucket for `wait_seconds` or 60 s.
  - `Transient` (HTTP 408/429/5xx, timeouts): backoff with jitter.
  - `Permanent` (any other non-zero status): fail the job with a category, never the provider
    text (ADR 0004 error rule).
- pydantic models per response, with unknown fields kept in `raw`.
- Endpoints: `getmeas`, `heart list/get`, `getactivity`, `getintradayactivity`, `getworkouts`,
  `sleep get/getsummary`, `user getdevice/getgoals`, `notify subscribe/list/revoke`,
  `oauth2 requesttoken/revoke`.
- **The base URL is per connection** (EU vs US medical cloud).
- Hand-written: the official OpenAPI file can't be fed to code generators (trailing-space paths).

### 4.3 OAuth and token vault
- `start`: create `state` bound to the session (10-min TTL, single use) and redirect with scopes
  `user.info,user.metrics,user.activity,user.sleepevents`.
- `callback`: validate `state`, then **exchange the code immediately (it's valid for 30 s)**, in
  the request, not in a job. Upsert the connection by Withings `userid` and refuse it if it's
  linked to another account.
- Tokens are envelope-encrypted (ADR 0004).
- Refresh is proactive when under 15 min remain, under a **per-connection advisory lock with a
  fencing check**. The rotated refresh token is written in the same transaction. A token call is
  never retried blindly.
- Disconnect: `notify revoke` for each appli, then `oauth2 revoke`, then delete the secret.
  Account delete runs the same steps before the purge.

### 4.4 Sync engine
| Stream | Backfill | Incremental | Webhook appli |
|---|---|---|---|
| `measures` | full history, 1-year windows | `getmeas lastupdate` (category 1 and 2) | 1, 2, 4, 58, 62, 63 |
| `ecg` | full, 3-month windows of `heart list`, then `heart get` per signal | list since cursor | 54, 55 |
| `activity_daily`, `workouts` | full history | `lastupdate` | 16 |
| `sleep_summary` | full history | `lastupdate` | 44 |
| `sleep_series` | last 90 days, more on demand | per night | 44 |
| `intraday` | last 30 days, more on demand (one call per day) | rolling | 16 |
| `devices`, `goals` | on connect | daily | 46 |

Job kinds:
- `withings_connect`: devices, goals, subscribe all applis (idempotent), enqueue backfills.
- `withings_backfill`: windowed, resumable, lowest priority, round-robin across users.
- `withings_sync`: one per connection and stream, singleton key; webhook bursts collapse into one
  run (30 s debounce).
- `ecg_fetch`: per signal.
- `withings_reconcile`: nightly. Re-read 30 days and mark rows missing upstream as `deleted_at`.
  Run `notify list` and re-subscribe on drift. Weekly full checksum of measures.
- `withings_poll`: every 6 h, safety net.

**Rate limiter**: a Postgres token bucket per Withings app (budget 100/min of 120), claimed in the
job claim transaction. Lanes, highest first: user "sync now" > webhook > poll > reconcile >
backfill. Status 601 pauses the bucket.

**Webhook receiver** (`/api/webhooks/withings/{secret}`):
- `HEAD` → 200.
- `POST`:
  1. Compare the path secret in constant time.
  2. Parse the form body.
  3. Map `userid` to a connection.
  4. Insert into `webhook_events` and enqueue `withings_sync`. `NOTIFY` wakes the worker.
  5. Return 200 in under 100 ms.
- Unknown user → 200 plus a log entry. The payload is only a hint; data is always fetched from
  the API.

**Idempotency**: upsert on natural keys (`grpid`, `signalid`, sleep id, activity date, workout
id), with a `payload_hash` to skip unchanged rows.

**M1 definition of done**
- [ ] Connecting a real account backfills its full history, with progress over SSE.
- [ ] A new weigh-in or ECG lands within about 2 min via webhook, with no manual action.
- [ ] Survives token expiry, rotation races, 601 storms, duplicate and out-of-order webhooks, and
      a worker crash mid-backfill (lease expiry → resume).
- [ ] Reconciliation catches an edited or deleted measure.
- [ ] Disconnect and account delete revoke upstream and purge local data.
- [ ] Findings §5 questions answered via the owner-run probe (§10.4) and recorded in an ADR.
- [ ] Tests: unit (parsers, `value×10^unit`, enums), contract (`fake-withings`), RLS guard tests on
      every new table, failure injection, e2e connect flow.

---

## 5. Data model (owned tables: `owner_id`, RLS forced, ADR 0002)

```
withings_apps        id, client_id, client_secret (enveloped), base_url, notify_path_secret_hash
connections          (classbeat table) provider 'withings', public jsonb {withings_userid, scopes, app_id, region}
sync_cursors         owner_id, connection_id, stream, lastupdate, backfill_state jsonb, last_ok_at, last_error
webhook_events       app_id, withings_userid, appli, startdate, enddate, received_at, job_id   (system table)
devices              owner_id, hash_deviceid, model_id, type, fw, battery, last_session_at
measure_groups       owner_id, grpid, measured_at, tz, attrib, category, model_id, hash_deviceid,
                     created, modified, deleted_at, raw jsonb, payload_hash
measures             owner_id, grpid, type, value_raw bigint, unit int, position int NULL
metric_catalog       type → key, label, unit, category, plausible_min/max, precision   (reference)
ecg_recordings       owner_id, signalid, recorded_at, model_id, afib, afib_details, heart_rate,
                     sampling_hz, wearposition, signal bytea (int32 µV, zstd), analysis jsonb
sleep_summaries, activity_daily, workouts                      (regular tables)
sleep_series, intraday                                         (monthly range partitions)
observations         owner_id, metric_key, ts, value, quality_flags, source_ref (monthly partitions)
metric_daily         owner_id, metric_key, day, morning, mean, min, max, n  (maintained by analysis jobs)
findings             owner_id, rule_id, rule_version, severity, metric_keys[], window, evidence jsonb,
                     status (new/seen/dismissed), first_seen, last_seen
scores               owner_id, kind, day, value, components jsonb
```
Volume check: one user's intraday minute data is about 0.5 M rows a year. Plain Postgres with
`(owner_id, ts)` B-tree plus BRIN indexes handles hundreds of users comfortably.

---

## 6. Identity and security (classbeat auth plus additions)

Already in classbeat:
- email and password
- Google OIDC
- TOTP and recovery codes
- server-side sessions with secure cookies
- rate limits
- email notices
- purge
- audit log

Additions:

| Addition | Approach |
|---|---|
| Mandatory 2FA | TOTP or passkey required before connecting Withings, and for every account holding health data |
| Passkeys | WebAuthn (`webauthn` library). Desktop browsers offer **"use a phone" via QR** (hybrid transport), which gives standard, phishing-resistant QR sign-in for free |
| QR approve-login | Desktop shows a QR with a 60 s single-use nonce bound to the desktop session cookie → a signed-in phone scans it → the phone shows the desktop's browser, OS, IP and city and asks to approve → desktop receives the session via SSE. Binding, a short TTL and explicit context prevent QR-jacking |
| Step-up | Fresh 2FA within 10 min for: connect/disconnect Withings, export, delete account, 2FA changes |
| Privacy | Consent record at connect, export (JSON/CSV/FIT), hard delete with upstream revoke, retention policy |

---

## 7. Analysis engine (worker jobs, Python)

### 7.1 Pipeline
1. **Normalize** raw rows into `observations` (metric key, SI value, local time, device,
   position).
2. **Quality gates**:
   - exclude `attrib=1` (ambiguous household measure) and show it as "unconfirmed"
   - plausibility ranges
   - collapse duplicates within 2 min
   - flag jumps over max(2 kg, 3 %) within 24 h
3. **Daily value**: the first morning weigh-in (04:00–11:00 local), otherwise the median.
4. **Baselines**: rolling 28-day median and MAD → robust z-score; EWMA (α≈0.1) trend line.
5. **Trends**: Theil–Sen slope with Mann–Kendall significance over 30/90/365 days → UP / STABLE /
   DOWN, against a per-metric minimal change (weight 0.5 kg/30 d, fat 0.5 pp, muscle 0.3 kg).
6. **Change points**: PELT (`ruptures`) → "something changed around <date>".
7. **Rules** produce versioned, de-duplicated **findings** with a cooldown, each carrying its
   evidence.
8. **Scores**:
   - Health Improvement Score from the score-card bands (findings §3)
   - Body Path trajectory (robust regression plus a prediction interval to the weight goal)
   - Readiness, only when sleep or watch data exists

### 7.2 Initial rule catalog (informational, not diagnosis)
**ECG** (types 130/135–138, heart list):
- E1 Withings AFib positive (`afib=1`, `afib_details` 1/11/12) → **warning**; show the strip and
  "share with a doctor".
- E2 ≥ 3 inconclusive or noisy ECGs in the last 10 → notice.
- E3 HR during ECG < 50 or > 100, or `afib_details` 6/7 → notice.
- E4 QTc > 450 ms (men) / > 460 ms (women) → notice; > 500 ms → warning. PR > 200 ms or QRS
  ≥ 120 ms → notice. All with a "single-lead estimate" caveat.
- E5 our irregularity index high (RR coefficient of variation, sample entropy, Poincaré SD1/SD2)
  while Withings says negative → notice.
- E6 drift vs the personal ECG baseline (QTc +30 ms, QRS +20 ms over 90 days) → notice.

**Nerve health** (167/196/229; `attrib 15` = guided scan):
- N1 "Insufficient data" until ≥ 4 measures over ≥ 4 weeks (like the app's PENDING).
- N2 a guided value, or 2 consecutive routine values, at robust z < −2 vs baseline → notice.
- N3 a significant 90/180-day decline → notice; monthly summary.
- N4 ESC (229, µS): < 40 severe, 40–60 moderate dysfunction (Sudoscan literature).
- N5 left/right asymmetry > 20 % when per-foot types exist.
- Routine and guided scans are trended separately.

**Body and cardio**:
- B1 +2 kg in 3 days or +2.5 kg in 7 days, especially with an extracellular-water rise
  → **warning** (fluid retention).
- B2 unintentional loss ≥ 5 % in 6 months → notice.
- B3 ECW / (ECW + ICW) above baseline or > 0.40 → notice.
- B4 muscle mass −2 % over 90 days with stable or falling weight → notice.
- B5 visceral fat (170) or metabolic age − age (227) trending up → info.
- B6 segmental left/right muscle asymmetry > 10 % → info.
- C1 resting HR +10 bpm vs baseline for ≥ 3 days, or < 50 / > 100 → notice.
- C2 vascular age − age above the score-card band, or rising → notice.
- Q1 no weigh-in for 14 days, low battery, connection needs reconnect → system notice.

### 7.3 ECG processing
µV signal + fs → 0.5–40 Hz band-pass + 50 Hz notch → R-peaks (NeuroKit2) → HR, HRV (RMSSD, SDNN,
pNN50) → irregularity and ectopic-beat detection → signal-quality index.

The viewer and PDF use a 25 mm/s × 10 mm/mV grid. With two leads, derive six: III = II − I,
aVR = −(I+II)/2, aVL = I − II/2, aVF = II − I/2.

### 7.4 AI insights (M6, opt-in, feature flag)
Weekly digest and a chat assistant on Claude, through classbeat's provider layer and eval
harness.
- Tools work over computed statistics (`get_series`, `get_findings`, `get_ecg_summary`,
  `get_scores`); no identifiers are sent.
- Explicit consent, a prompt/response audit, and egress allowlisted.

---

## 8. MVP UI (M5; replaceable)
React SPA from classbeat's skeleton (API client, SessionContext, GuardProvider, design tokens).
Charts: uPlot or ECharts; ECG on canvas.

Pages:
- Onboarding (account, 2FA, connect, backfill progress)
- Dashboard
- Metric detail
- ECG list and viewer (+PDF)
- Nerve health
- Body composition (segmental map)
- Findings inbox
- Settings (connections, security, export, delete)

The SPA talks only to `/api`.

---

## 9. Environments, hosting and legal

### 9.1 Local development (Windows, Docker Desktop)
- `compose.yaml`: web, worker, postgres, mailpit, fake-oidc, fake-withings.
- Worktrees in `../ownvitals-wt/`; shell scripts LF-only (as in classbeat).
- Real Withings only through the `ownvitals-dev` app. If Withings refuses a `localhost` redirect,
  use `withings-dev.pernemark.se` through SWAG to your workstation, or a cloudflared tunnel.

### 9.2 Home server (private phase)
| Piece | Setting |
|---|---|
| App host | `new-home-server` 192.168.1.222 (Ubuntu 26.04, 8 cores, 14 GB RAM, Docker 29). Compose project `ownvitals`, image from GHCR (`release.yml` on tag `v*`) |
| Ports | web bound to **`192.168.1.222:8096`** (8091–8095 are taken). Postgres not published, or `127.0.0.1` only |
| Reverse proxy | SWAG on 192.168.1.151: new `site-confs/ownvitals.conf`, `server_name withings.pernemark.se`, `include /config/nginx/ssl.conf`, `proxy_pass http://192.168.1.222:8096` plus the usual forwarded headers (same as `24c.conf`). The wildcard `*.pernemark.se` certificate (Cloudflare DNS-01) already covers it |
| DNS | Cloudflare: `withings` CNAME to the record ddclient keeps updated (manual, by you) |
| Cloudflare proxy | If proxied (orange cloud): SSL "Full (strict)", and a **WAF skip rule for `/api/webhooks/withings/*`** so bot protection never challenges Withings' HEAD/POST. Trust `CF-Connecting-IP` only from SWAG |
| Host hardening | Host firewall: allow 8096 only from 192.168.1.151; own Docker network; KEK files mounted read-only and kept outside the database; nightly encrypted `pg_dump` to another machine; quarterly restore drill |
| Shared-host risk | The same host runs a torrent client, flaresolverr, Portainer (Docker socket = root) and others. Acceptable for private use; it's the main reason to move before going public |

### 9.3 Legal (not legal advice)
- **Private phase (you and your household)**: GDPR doesn't apply to purely personal or household
  activity (Art. 2(2)(c)). Home hosting is fine. With friends outside the household, still get
  explicit consent and honour deletion requests.
- **Public phase**: you become the controller of Art. 9 health data. That requires:
  - explicit consent (Art. 9(2)(a)) and a privacy notice
  - a record of processing
  - a **DPIA** (Art. 35, likely mandatory for health data)
  - security proportionate to the risk (Art. 32)
  - breach reporting to IMY within 72 h, and data-subject rights

  Home hosting isn't forbidden, but Art. 32 is hard to argue on a shared home box. **Move to an
  EU VPS** (e.g. Hetzner, or Azure Sweden Central) before opening sign-ups, and check your ISP's
  terms for hosting.
- Also before going public: Withings developer terms, MDR wellness wording, a host name without
  the Withings trademark (e.g. `ownvitals.pernemark.se`).

---

## 10. Agentic development (classbeat's AGENTS.md model)

### 10.1 Roles
| Role | Model | Does |
|---|---|---|
| Owner (you) | — | Product decisions, external approvals: Withings app registration, Cloudflare/SWAG changes, production deploys, the probe run (§10.4) |
| Architect / Orchestrator | Opus (main session) | Splits each milestone into issues (one per PR, with acceptance criteria and "Depends on #n"), writes ADRs, merge order, `docs/status.md` |
| Developer | Sonnet; **Opus for security-critical issues** | One issue → one PR in its own worktree; code, tests, CI fixes, all review findings |
| Explorer | Haiku | Bounded work: fixtures, `fake-withings` payloads from the public spec, docs research |
| Reviewer | **Fresh context per review**: Opus for security-critical, Sonnet otherwise | Reviews the diff at one head SHA, then posts `Agent review (<model>): PASS/FAIL <sha>` on the PR |

**Security-critical here**:
- token vault, refresh rotation and OAuth state
- webhook receiver
- RLS and UserContext on every health table
- jobs, leases and the rate limiter
- auth additions (passkeys, QR login)
- export and delete
- AI egress of health data

### 10.2 Loop and merge gate
```
milestone → issues → Developer (worktree) → PR → CI → fresh Reviewer
   PASS <sha> → gh pr merge --squash --match-head-commit <sha>
   FAIL       → Developer fixes all blocking findings → CI → delta review
```
- Every push after a PASS needs a new (delta) review.
- Agents never `--admin`, `--auto`, approve, or override a FAIL.
- You can always merge on GitHub yourself.
- Health-specific review checks:
  - no real health data in tests, fixtures or logs
  - units (`value×10^unit`) and time zones correct
  - each rule threshold cites its source
  - wellness wording

### 10.3 GitHub setup (public repo)
- **GitHub-hosted runners are free for public repos**, which solves the exhausted Pro minutes.
- Don't use a self-hosted runner on the home server: on a public repo, fork PRs could run code
  on it.
- Rulesets as in classbeat:
  - `main` requires a PR plus green CI, squash only, linear history, branches deleted after
    merge
  - protected `v*` tags
- Plus: CODEOWNERS `@magpern`, Dependabot, commitlint PR titles, secret scanning and push
  protection, and nothing under `secrets/` ever committed.
- Release: tag `vX.Y.Z` → image to GHCR. Deploy is `docker compose pull && up -d` on the home
  server, run by the Architect over ssh **only after your go-ahead** for each deploy.

### 10.4 Real data without exposing it to agents
Agents build against `fake-withings` and the demo account. The open questions (findings §5) are
answered by an **owner-run probe**: `ownvitals probe --connection <id>` prints only **shapes**
(meastypes present and their counts, ECG lead count, signal length, sampling rate, attrib and
position values seen, notification fields). It prints no values or timestamps. You paste the
output into the issue.

---

## 11. Milestones

| Milestone | Scope | Done when |
|---|---|---|
| M0 Platform | Repo, port classbeat platform (auth, crypto, connections, RLS, jobs, worker, admin, CI, agents, e2e harness), rename, ADR 0001–0005 adapted, compose for dev | CI green; login, TOTP and Google work in e2e; RLS guards pass |
| M1 **Withings core** | §4 + `fake-withings` + probe CLI | M1 checklist (§4.4) |
| M2 Identity additions | Passkeys, QR approve-login, mandatory 2FA, step-up, export/delete | Security review (Opus) PASS; e2e for each flow |
| M3 Analysis v1 | Normalize, daily values, baselines, trends, change points, B/C/Q rules, Health Score | Rules tested on synthetic series; findings reproducible on your data |
| M4 ECG & nerve health | ECG pipeline + viewer data, nerve model, E/N rules | Validated on your ECG history (owner-run), false-positive review |
| M5 MVP UI | §8 | A second (family) user completes onboarding unaided |
| M6 AI & reports | Digest, assistant, doctor PDF, CSV/JSON/FIT export | Opt-in flow, evals green, audit trail |
| M7 Home deploy | §9.2 complete, backups and a restore drill, monitoring (existing Grafana/Prometheus on the home server) | Running at `withings.pernemark.se`; restore drill passes |
| M8 Go-public gate | VPS move, DPIA, privacy notice, terms review, quotas, load test of the rate limiter | Owner sign-off |

M7 can start once M1 is merged, so real webhooks reach the home server early. M2 and M3 can run
in parallel after M1.

---

## 12. Risks
| Risk | Mitigation |
|---|---|
| Public API returns one ECG lead (Body Scan records 6) | Probe in M1; viewer supports 1..n leads |
| Nerve-health diagnosis is proprietary (`libsudo`) | Personal-baseline model; Withings' score shown as-is; no diagnostic claims |
| 120 req/min shared by all users | Webhook-first, lanes, lazy intraday backfill, BYO app, Enterprise plan if needed |
| Missed webhooks or Cloudflare blocking them | Poll + reconcile; WAF skip rule; alert on "no webhook for N days while data changed" |
| Refresh-token rotation races | Advisory lock + fencing + transactional write |
| Shared home host compromise | Firewall, isolation, KEK off-database, move before public (§9) |
| Divergence from classbeat after the copy | Pinned source SHA in ADR 0001; port security fixes deliberately |
| Regulation (GDPR Art. 9, MDR) | §9.3 gates before public launch |
