# Threat model: health data

Status: initial M0 version (#4, 2026-10-31), written before the port. #16 updates it against the
merged code; the human security review of the M8 gate re-checks it end to end. Nothing here claims
a status beyond what D10 allows: no control is INTEGRATION_VERIFIED or PRODUCTION_READY.

## How to read it

- It starts from classbeat's threat model at the pinned commit
  `d80605100992f85f8f02672b9b5f885ed6166214` and re-assesses every ported component for
  **special-category health data** (PLAN §3). The "From" column names the classbeat threat a row
  adapts (`cb T5`) or says `new`. What was dropped is listed at the end.
- **Enforced by** names the issue and the test that enforce a mitigation in M0, using the test
  names the M0 issues port; the milestone and PLAN section for a mitigation that comes later
  (`M1: …`); `owner` for an owner action; or `accepted` (see "Accepted risks"). #16 replaces every
  planned test name with the merged one.
- Open items (`O1` …) are listed at the end with who decides them.
- ADR references are to [docs/adr/](../adr/README.md); §-references without a file are PLAN.md,
  §9.x and §10.x are PLAN-operations.md.

## What we protect

| Asset | Where | Why it matters |
|---|---|---|
| **Health data**: weight and body composition, ECG recordings and signals, nerve-health values, blood pressure, temperature, sleep, activity, intraday series, raw payloads | owned tables (PLAN §5), backups | GDPR special category (Art. 9); a leak can't be undone, can reveal conditions (AFib, signs of neuropathy) and usually concerns another household member |
| **Derived health information**: observations, daily values, findings (experimental tier included), scores, reports; AI conversations from M7 | owned tables | as above; an experimental finding can also read as a medical claim (D9) |
| **Health metadata**: webhook events (who measured what, when), job inputs, sync cursors, devices, Withings user ids | system and owned tables, logs, metrics | reveals that and when someone took an ECG or stood on the scale |
| Withings access and refresh tokens | `connections`, enveloped | read access to the user's whole Withings history; the refresh token lives a year |
| Withings app client secret, webhook secrets, the pepper | `withings_apps` (enveloped, HMACs), secret files | the client secret mints and revokes tokens; a webhook secret triggers fetches |
| KEKs | secret files on the host, backed up separately | open every token, seed and the client secret |
| TOTP seeds and recovery codes | account tables (enveloped, HMACs) | the second factor of every account |
| Accounts, passwords, sessions, identities | account tables | a takeover reaches everything above |
| Admin and operator powers | admin area, CLI, deploy key | flags (experimental tier, AI), job replay, key rotation and wipe, deploys |
| Repository, CI, release images, deploy key | GitHub, GHCR, home server | a compromised build reaches every user's data |
| **Correctness and availability of the data** | the sync engine | missing data shown as "normal" misleads about someone's health (PLAN §4.2) |

## Who might attack, or fail

- **Anyone on the internet**: the landing and sign-in pages, and the webhook URL pattern, which
  the public repository documents.
- **A webhook forger** who knows the pattern but no secret; or whoever obtained a secret (a
  Withings-side leak, a proxy log).
- **A curious household member** with an account, trying another member's data: the realistic
  insider of the household phase.
- **A taken-over account**: a phished or reused password, a session left open on a shared
  household computer.
- **A co-tenant of the home server**: the torrent client, flaresolverr, Portainer (which holds the
  Docker socket), or whoever compromises one of them (§9.2).
- **Infrastructure in the path**: Cloudflare (if proxied) and SWAG terminate TLS.
- **Withings as a provider**: retries, delays, undocumented values, unannounced deletions, changed
  behaviour, the Withings+ rule for new accounts (§9.4).
- **A compromised dependency, base image or GitHub Action**; a malicious release.
- **Our own development agents**: mistakes, prompt injection through issues and upstream text;
  they must never see real health data.
- **The owner as operator**: a wrong command, a lost KEK or backup.
- From M7: **the model provider and model output**.

## Trust boundaries

```
browser ──TLS──▶ Cloudflare ──TLS──▶ SWAG 192.168.1.151 ──HTTP on the LAN──▶ web :8096
Withings ──webhook POST/HEAD──▶ Cloudflare ──▶ SWAG ─────────────────────▶ web (receiver)

shared home host 192.168.1.222 (also a torrent client, flaresolverr, Portainer)
│  web ─────┬── Postgres 17 (app role, forced RLS; not published)
│  worker ──┘
│  web ───── egress allowlist ──▶ Google sign-in; from M1 the Withings code exchange only
│                                 (`oauth2 requesttoken`, roadmap Amendment 1)
│  worker ── egress allowlist ──▶ wbsapi.withings.net, account.withings.com,
│                                 api.anthropic.com (M7, opt-in only)
│  web, worker ── SMTP ──▶ mail relay
│  KEK and pepper files (read-only, outside the database volume)

GitHub ── Actions ── GHCR image + attestation ── forced-command deploy key ──▶ home server
backups ── encrypted dump ──▶ another machine; KEKs backed up separately
development machine: agents, fakes and the demo account only; no real health data
```

- Everything crossing into web or worker is validated there: browser input, webhook fields,
  Withings responses (unknown fields kept in `raw`, never trusted as instructions or URLs) and,
  from M7, model output.
- **Inside the shared host, containers are not a boundary** against a co-tenant that controls the
  Docker socket: such a co-tenant is root on the host and reaches the database, the KEK files and
  every secret (T60).
- The SWAG → web hop carries plaintext on the home LAN; the host firewall admits port 8096 only
  from SWAG (§9.2).

## What changes from classbeat, for health data

- **Impact.** Any confidentiality failure discloses special-category data, usually about another
  household member. GDPR doesn't apply in the household phase (§9.3), but the controls are built
  as if it did, so the M8 gate assesses rather than redesigns.
- **A password alone reaches nothing** (T4); sensitive actions need step-up (T6).
- **Admins can't read health data** (T26); classbeat's maint role could read allowlisted content.
- **Health data at rest is not envelope-encrypted**; only tokens, seeds and the client secret are.
  It is protected by RLS and the role design, host hardening and encrypted backups (T60, T61,
  accepted risk 2).
- **A new unauthenticated inbound channel**, the Withings webhook (T38 to T43).
- **Long-lived upstream credentials with rotation hazards**: a refresh token valid for a year,
  rotated on every refresh (T35).
- **Correctness is a safety property**: missing data must never read as zero or normal (T43, T72).
- **No health data through side channels**: logs, metrics, mail, job events, model input (T47,
  T57 to T59, T73).
- **Agents never see real data** (T67), and the home server is shared with other services (T60).

## Threats and mitigations

### A. Accounts, sessions and step-up ([ADR 0003](../adr/0003-accounts-and-sessions.md))

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T1 | Takeover by matching an attacker's Google account to the victim's email | Identity key `(google, sub)`, never the email; a verified Google email matching an existing user creates and links nothing; linking needs a signed-in session, recent auth and step-up (ADR 0003 §1, §4) | #10 `test_auth_oauth_signin.py`, `test_auth_oauth_link.py` | cb T1 |
| T2 | Login CSRF, OAuth state or code injection at sign-in | State, PKCE (S256) and nonce in a 10-minute flow row taken atomically before the code exchange; the callback bound to the browser's flow cookie; no sign-in from a mail link (ADR 0003 §1) | #10 `test_auth_oauth_signin.py`, `test_auth_csrf.py`; #14 Google e2e | cb T2 |
| T3 | Credential stuffing and password brute force | argon2id; failure-only limits per (account, IP), per account and per IP, reserved before the check; no lockout; invite-only sign-up; a correct password alone still reaches no data (T4) | #10 `test_auth_ratelimit.py`, `test_auth_signin.py`; #11 `test_auth_mandatory_totp.py` | cb T3 |
| T4 | A phished or reused password reaching health data | TOTP mandatory for every account; a session without a verified second factor never yields a `UserContext` and reaches only the verify/enrol allowlist; it is deleted after 10 minutes; a test enumerates every route; the SPA's guards are convenience, the API enforces (ADR 0003 §2) | #11 `test_auth_mandatory_totp.py`; #3 guard tests; #14 e2e sign-up → forced enrolment → sign-in with TOTP | new |
| T5 | Session theft or fixation | Sessions server-side, stored as SHA-256; `__Host-` cookie, `HttpOnly`, `Secure`, `SameSite=Lax`; rotation on sign-in, second factor, re-auth, step-up and privilege change; idle and absolute limits; revocation rules; Origin check and CSRF token on writes (ADR 0003 §1, §3) | #10 `test_auth_sessions.py`, `test_auth_csrf.py`; #11 `test_auth_sessions_protection.py` | cb T4 |
| T6 | A stolen live session (shared household computer, unlocked laptop) used to connect or disconnect Withings, delete the account, change email or password, swap the second factor or log the owner out | Step-up, a TOTP or recovery code within 10 minutes, for PLAN §6's list, plus recent authentication where classbeat has it; refusals `step_up_required`, audited (ADR 0003 §4) | #11 `test_auth_stepup.py` (one test per action); #13 connect and disconnect refused without step-up; #14 step-up e2e | cb T27 |
| T7 | Guessing or replaying a TOTP or recovery code, at sign-in or at step-up | Replay refused by the conditional `last_step` update; recovery codes single-use; MFA failure buckets (5 per 5 min per account and IP, 20 per hour per account); failures audited, five in an hour mailed (ADR 0003 §1, §2) | #10 `test_auth_mfa_verify.py`, `test_auth_mfa_hardening.py`, `test_auth_ratelimit.py`; #11 `test_auth_stepup.py` | new |
| T8 | TOTP seeds or recovery codes read from a dump or backup | Seeds under the envelope (AAD bound to the user, KEK outside the database); recovery codes as HMACs (ADR 0003 §5, ADR 0004 §2) | #8 `test_envelope.py`; #10 `test_totp_envelope.py`; #13 `test_keys_rewrap.py` | cb T35 |
| T9 | Account enumeration through sign-in, sign-up, reset or timing | One message per flow; "check your inbox" either way; a dummy argon2 verification; mail sent after the response (ADR 0003 §1) | #10 `test_auth_signin.py`, `test_auth_signup.py` | cb T33 |
| T10 | A reset or verification link leaking or reused | 32 random bytes, SHA-256 stored, single use, short-lived, in the URL fragment; a reset revokes every session; mail over TLS except to loopback or mailpit | #10 `test_auth_reset.py`, `test_auth_email.py`; #6 `test_mail.py` | cb T34 |
| T11 | Open redirect after sign-in | `return_to` only as a relative path, checked again before use | #10 `test_auth_oauth_signin.py` | cb T36 |
| T12 | Host-header poisoning of links in mail | Links built only from `OWNVITALS_PUBLIC_BASE_URL` | #10 `test_auth_email.py` | cb T37 |
| T13 | A forged Google ID token, or a test fake reachable in production | RS256 only against Google's JWKS; `iss`, `aud`/`azp`, `exp`, `iat`, `nonce` checked; one provider and callback path; `fake-oidc` configurable only with `OWNVITALS_ENVIRONMENT=test` (ADR 0003 §1) | #10 `test_oauth_idtoken.py`; #14 (fake only under the `test` profile) | cb T38 |
| T14 | Account pre-hijacking | No unverified accounts; password typed again at confirmation; a provider sign-up with an unverified email confirms only in its own browser; every link mailed (ADR 0003 §1) | #10 `test_auth_signup.py`, `test_auth_oauth_handoff.py` | cb T39 |
| T15 | Email abuse: sign-up spam, reset flooding | Invite codes; per-address and per-IP mail limits | #10 `test_auth_ratelimit.py`, `test_auth_signup.py` | cb T24 |
| T16 | Sign-up opened beyond the household before the M8 gate (D7) | `open` refused by setting validation; the stored flag can only tighten (ADR 0003 §6) | #10 `test_flags.py`; #12 `test_flags_db.py` | new |
| T17 | A client choosing its own IP to escape or poison rate-limit buckets | Peer address unless the peer is a trusted proxy (SWAG); `CF-Connecting-IP` only from SWAG; port 8096 open only to SWAG; SWAG sets that header only for Cloudflare's ranges (ADR 0003 §8) | #10 `test_auth_ratelimit.py` (client-IP cases); owner, M2 (O6) | new |
| T18 | A lost authenticator locking a user out, or the recovery path used for a takeover | Recovery codes; `admin mfa-reset` only from the CLI (host access), audited, revokes sessions, mails a reset link; first enrolment race accepted (accepted risk 6) (ADR 0003 §2) | #10 `test_auth_cli.py`, `test_auth_mfa.py` | new |

### B. Tenant isolation ([ADR 0002](../adr/0002-tenant-isolation.md))

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T19 | Cross-tenant read or write of health data (a missing filter, an id from the browser) | `UserContext` everywhere, repository filters, forced RLS with one permissive policy, `UNIQUE (owner_id, id)`, owner-inclusive foreign keys, 404 for foreign ids (ADR 0002 §1, §2) | #5 `test_rls_guard.py`, `test_two_user_isolation.py`, `test_owner_filter.py`, `test_isolation.py`; guard 7 per route from #10 | cb T5 |
| T20 | A webhook, scheduler, worker, admin or CLI context reaching another user's rows | No ambient authority: webhook and scheduler create jobs only inside definer functions that return no owner id; contexts issued only by auth (after TOTP) and from a claimed job row; RLS tests per context (ADR 0002 §4, ADR 0005 §4) | #5 harness; #9 worker; #15 `test_isolation_contexts*.py` and its table-enumerating guard; M1 webhook and scheduler functions | cb T5 |
| T21 | Code forging, copying or deserialising a `UserContext` | Issuer token checked by identity; copy, replace, pickle and Pydantic construction refused; AST guard (ADR 0002 §1) | #5 `test_context.py`, `test_user_context_guard.py` | cb ADR 0002 |
| T22 | A pooled connection carrying one user's setting, cursor or state to the next | Transaction-local `set_config`; `user_session` owns the transaction; `DISCARD ALL` on return; checkout check (ADR 0002 §6) | #5 `test_session_guard.py`, `test_db.py` | cb ADR 0002 |
| T23 | Web or worker running with a role that bypasses RLS | Startup and per-connection refusal of superuser, `BYPASSRLS`, dangerous memberships and ownership, no override; the owner password never on web or worker (ADR 0002 §5) | #5 `test_role_check.py`, `test_roles_script.py`, `test_roles_rerun.py` | cb ADR 0002 |
| T24 | SQL injection: the app role can set `app.user_id`, so injected SQL defeats both layers | Bound parameters only, no SQL built from strings (ruff's rule); no `TRUNCATE`, `MAINTAIN` or ownership; role-default timeouts checked at startup and per connection (residual denial of service until detected) (ADR 0002 §5) | #2 ruff settings; #5 guard 2 (`role_guard.py`) and the startup check | cb T26 |
| T25 | Learning or blocking another user's rows through a unique index or foreign key | Owner-inclusive unique indexes and foreign keys; the single exception, `connections (app_id, withings_userid)`, reveals only that a Withings account the caller controls is linked elsewhere (ADR 0002 §2) | #5 guard 1 (`rls_guard.py`); M1 allows the exception by name | cb ADR 0002 |
| T26 | An admin, or a stolen admin session, reading health data | `ownvitals_maint` has no privilege on any health table; column-level allowlist without secret columns; read-only by default; every use audited (ADR 0002 §4) | #12 `maint_guard.py`, `test_maint_privileges.py` | new |
| T27 | `NOTIFY` payloads read by any database listener | Only ids; data read afterwards in a user session (ADR 0005 §7) | #9 `test_jobs_events.py`, `test_queue_functions.py` | cb T30 |
| T28 | A monthly partition of a health table read directly, around its parent's policy, or a partition created without RLS | Parent and every partition with forced RLS and the `owner_isolation` policy; the app role has privileges on the parent only (`REVOKE ALL` on each partition); partitions created only by the reviewed, pinned `ensure_month_partitions` function, called by the scheduler (ADR 0002 §2) | #5 guard 1 refuses partitioned tables until #21; #21 extends guard 1 to check the parent and every partition, and tests the function | new |

### C. Keys, tokens and connections ([ADR 0004](../adr/0004-connections-and-encryption.md))

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T29 | Withings tokens read from a database dump or backup | Envelope encryption: a data key per write, AES-256-GCM, AAD naming owner, provider and row, KEK in a file outside the database, a check value against a swapped KEK; backups hold ciphertext (ADR 0004 §2, §3) | #8 `test_envelope.py`, `test_keks.py`; #13 `test_connections.py` (`pg_dump` test) | cb T6, T40 |
| T30 | A token leaking through logs, errors, job rows, events or SSE | Only `ownvitals.connections` decrypts; a redacting credentials object; jobs hold the connection id; `ConnectionFailure` without cause or context; masked log keys (ADR 0004 §5, §8, §9) | #13 `test_connections.py`, `test_connection_errors.py`; #2 `test_logging.py` | cb T41 |
| T31 | One user's tokens used for another user's sync | Owned table read through `UserContext`; credentials rebuilt from the claimed job's owner; moved ciphertext fails its AAD (ADR 0004 §2, §5) | #8 wrong-AAD tests; #13; #15 worker context; M1 two-user test with `fake-withings` recording each call's token | cb T42 |
| T32 | A KEK, or the whole secrets directory, leaked | Everything goes: stop, new KEK, `keys wipe` with the services down (connection rows to `needs_reconnect` without credentials, TOTP reset), older backups treated as compromised; the owner rotates the Withings client secret so stolen refresh tokens are useless, and the pepper with every webhook secret; no upstream revoke (stolen access tokens live up to 3 h: accepted risk 19) (ADR 0004 §3) | #13 `test_keys_wipe.py`; owner (runbook, M1) | cb T40 |
| T33 | The current KEK lost | KEKs backed up separately from the dumps; restore drill; without the KEK every user reconnects and re-enrols, by design | owner, M2 (backups, restore drill) | new |
| T34 | A stale refresh, reconnect or 401 overwriting newer tokens or flipping a fresh connection | `token_generation` raised by every token write; token writes and status changes are compare-and-swap on the generation read; rewrap doesn't change it (ADR 0004 §6) | #13 generation CAS test; M1 PLAN §4.3 tests | cb T77 |
| T35 | Losing the only valid refresh token, or using the new access token before the new pair is saved | PLAN §4.3: per-connection lock, durable intent, CAS write, first use after the commit, one recovery retry within the 8-hour grace, then `needs_reconnect`; never the generic retry (ADR 0004 §6) | M1: PLAN §4.3's crash and race tests | new |
| T36 | Withings OAuth abuse: forged state, code injection, a victim's session finishing an attacker's flow, a Withings account already linked to another ownvitals account | State hashed, bound to the session, 10 minutes, single use, taken before the exchange; the code exchanged in the request through web's one narrow egress entry (roadmap Amendment 1); a Withings user linked to another account refused, and a different Withings account refused while one is connected; connect needs recent auth and step-up; `fake-withings` only in tests (ADR 0004 §7) | #13 (step-up on connect); M1 OAuth tests | new |
| T37 | The Withings app client secret leaking | Enveloped in `withings_apps` with AAD bound to the app; writes admin-only; never logged; rotated by the owner at Withings after any suspected leak (ADR 0004 §2, §3) | M1; owner | new |

### D. The Withings webhook (PLAN §4.4; ADR 0002 §4, ADR 0004 §4, ADR 0005 §8)

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T38 | Forged notifications from anyone who knows the URL pattern | A 256-bit secret per app in the path, stored only as a peppered HMAC, compared in constant time; an unknown secret refused before any database work (O14); the payload is only a hint: data is always fetched from the API with the user's own token | M1 webhook fuzzing, forged (PLAN §10.3) | new |
| T39 | Replayed or flooded notifications with a valid (leaked) secret, burning the shared 120/min budget | Dedupe key; singleton jobs with debounce; per-connection and per-app quotas in the ingest function; lanes keep "sync now" first; the worst case is rate budget, never data; rotation with two active secrets | M1 webhook fuzzing (replayed, duplicate), 601-storm test | new |
| T40 | Oversized or malformed bodies against the parser | `HEAD` → 200; `POST` only form-encoded, over 2 KB refused before parsing, strict fields with sane date bounds | M1 webhook fuzzing (oversized, malformed) | new |
| T41 | The secret leaking through logs or traces | Path redacted in app logs, traces and errors; SWAG's webhook location logs without the URI; log-redaction tests. Cloudflare, if proxied, still sees it (accepted risk 5) | M1 log-redaction tests; owner, M2 (O6) | new |
| T42 | Probing the webhook to learn which Withings users are linked | A generic 200 for an unknown user; internal logging rate-limited | M1 | new |
| T43 | Missed, late, duplicated or out-of-order notifications leaving data incomplete while the UI implies "nothing" or "normal" | Sync cursors, not webhooks, define correctness; poll every 6 h and reconcile nightly; deletions only after two complete reconciliations; per-stream completeness and capability states; missing data never shown as zero (D3, PLAN §4.2, §4.4) | M1 checklist (PLAN §4.4) | new |

### E. Jobs, worker and egress ([ADR 0005](../adr/0005-jobs-and-leases.md))

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T44 | The queue's definer functions used to read or move other users' jobs | Three functions owned by `NOLOGIN` `ownvitals_queue`, fixed `search_path`, no caller ids, minimal returns; installed by the superuser script; md5-pinned startup check; no other user code in the database (ADR 0005 §3) | #9 `test_queue_functions.py`, `test_queue_guard.py` (guard 9) | cb T45 |
| T45 | A worker that lost its lease writing job state or health rows | The fencing token checked first in every worker transaction and held to the commit; health rows, revisions and cursors written in fenced transactions (ADR 0005 §4) | #9 `test_jobs_fence.py`, `test_jobs_worker.py`; M1 crash mid-backfill test | cb T12 |
| T46 | A repeated request creating duplicate jobs | `Idempotency-Key` with a request hash, 409 on another body (ADR 0005 §2) | #9 `test_jobs_api.py` | cb T46 |
| T47 | Job events or SSE leaking another user's progress, secrets or health values | Owned events read per request; 404 for foreign ids; payload models allow enums, own ids, counts and dates only (ADR 0005 §7) | #9 `test_jobs_events.py`, `test_jobs_stream.py` | cb T48 |
| T48 | Web or worker reaching arbitrary hosts (SSRF, exfiltration) | One module builds every client; per-process allowlists (worker: Withings hosts, Anthropic only with AI on; web: Google sign-in and, from M1, only the Withings authorization-code exchange, roadmap Amendment 1); https, port 443, every redirect hop checked; total deadlines and request budgets; no TLS key log; no URL from user or provider input is fetched | #6 `test_egress.py`, `test_egress_guard.py`, `test_egress_deadline.py`, `test_egress_budget.py`, `test_egress_runtime.py` | cb T19, T49 |
| T49 | A poison job or retry storm exhausting the shared Withings budget for everyone | Retry budget, capped backoff, non-retryable categories, poison rows failed by the reaper; M1: tokens consumed when claimed and never refunded, 601 pauses the bucket, refresh never retried generically (ADR 0005 §6, §8) | #9 `test_jobs_worker.py`, `test_jobs_errors.py`; M1 601-storm and crashed-call tests | cb T50 |
| T50 | A job continuing for a disabled or deleted user | Owner checked after every claim; jobs cascade with the user, so the fence fails on the next write (ADR 0005 §4) | #9 `test_jobs_runner.py` | cb T53 |
| T51 | One user's backfill starving another's sync | Lanes, per-owner fairness, a capped backfill share (ADR 0005 §8) | M1 | new |

### F. Admin, flags and the browser

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T52 | Admin account compromise | TOTP for every account; the admin role only with TOTP within 12 h; step-up within 5 min for admin writes; role changes from the CLI only; every admin route guarded | #12 `test_admin_routes_guard.py`; #11 | cb T15, T90 |
| T53 | Admin misuse, or the audit log altered to hide it | Every admin write audited in its own transaction, refusals too; rows hold keys and meanings, never secrets, tokens or health values; append-only for maint | #12 `test_admin_audit.py` | cb T16, T92 |
| T54 | A flag loosening a protection: the experimental tier beyond the household, AI on without consent, open sign-up | Stored flags joined with the environment, stricter wins; `experimental_findings` owner-only, default off (D9); `ai_insights` default off (D11); unknown keys refused; invalid values fall back to the stricter default | #12 `test_flags_db.py`; #10 `test_flags.py` | cb T94 |
| T55 | Script injection through user, admin or provider text (a banner, device names, Withings fields) | React escaping only, no raw HTML; CSP `'self'` only, no inline script or style, `frame-ancestors 'none'`, `Referrer-Policy: no-referrer`; no third-party host at runtime | #2 `test_web.py` (headers); #3 third-party-host guard | cb T20, T95 |
| T56 | Cross-site requests, or health data cached by a browser or proxy | Origin check and CSRF token on every write; `Cache-Control: no-store` on every API response | #10 `test_auth_csrf.py`; #12; #9 `test_jobs_stream.py` | cb T93 |

### G. Logs, metrics and mail

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T57 | Health values, tokens or secrets in logs, errors or traces | Secrets only from files, as `SecretStr`; JSON logs with a redaction filter; no exception text in responses; logs never hold measurement values, ECG samples, tokens, webhook secrets or full webhook paths (AGENTS.md) | #2 `test_logging.py`; #10 `test_auth_logging.py`; #13; M1 log-redaction tests (PLAN §10.3) | cb T7 |
| T58 | Health values or user identifiers exported as Prometheus metrics | Labels limited to app, stream and category; no user id, Withings user id or value in a label (proposed) | M1 (O7) | new |
| T59 | Health data in emails, which pass through a relay and mailboxes outside our control | Notification mails say something is waiting and link to the app; no values, finding text or ECG results (proposed) | M4/M6 (O8) | new |

### H. Hosting, supply chain and agents

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T60 | Compromise of the shared home host (torrent client, flaresolverr, Portainer with the Docker socket) exposing the database, KEKs and secrets | Host firewall (8096 only from SWAG), own Docker network, Postgres unpublished, KEK files read-only outside the database, a deploy key without shell; accepted for the household only (accepted risk 1); EU VPS before M8 (§9.2, D8) | owner, M2; M8 | new |
| T61 | Backup theft or loss | Nightly encrypted `pg_dump`, KEKs backed up separately to another machine; RPO 24 h, RTO 4 h; quarterly restore drill; the real-data gate needs a passing drill (§9.2) | owner, M2 | new |
| T62 | A malicious or tampered image deployed | Protected `v*` tags; `release.yml` attests build provenance; the forced-command key accepts only an attested digest from such a tag, pulls by digest, has no shell; deploys only on the owner's go-ahead (§9.2) | #7 attestation; owner, M2 deploy script | new |
| T63 | Exposed services or container breakout | Non-root image; local ports on 127.0.0.1; Postgres never published; only SWAG reaches web | #2 compose; #14 stack check (non-root, no secret in `docker inspect`) | cb T21 |
| T64 | Cloudflare and SWAG seeing plaintext (pages, API responses, webhook URLs); plain HTTP from SWAG to web on the LAN | SSL Full (strict); firewall; accepted for the household (accepted risk 4); Cloudflare is a processor question at M8 (§9.3) | owner, M2 (O12); M8 | new |
| T65 | A compromised dependency, base image or Action | Lockfiles, exact versions, SHA-pinned Actions, checksum-verified tools, Dependabot, least-privilege `permissions`, `persist-credentials: false`; Trivy image scan | #1 CI; #7 Dependabot; #14 Trivy | cb T17 |
| T66 | An agent committing a secret or weakening a check | gitleaks on each PR's commits and weekly on the history; frozen-spec guard; the review gate; the ruleset's required checks | #1 `ci` / `checks` | cb T18 |
| T67 | Real health data reaching agents, fixtures, tests, logs, PRs or issues | No real data on the development machine; synthetic, labelled fixtures; `fake-withings` and the demo account only; the probe prints shapes only and the owner runs it; agents never connect a real account (AGENTS.md, §10.4) | #1 AGENTS.md and review checks; M1 probe tests; O9 | new |
| T68 | Prompt injection of the development agents through issues, PRs or upstream text | Repository and web content treated as data; the review gate and ruleset on every merge; no secret in any prompt | #1 AGENTS.md, `.claude/agents/` | cb T31 |
| T69 | The public repository telling an attacker the design | No control relies on secrecy of the design; secrets never in the repository | #1 gitleaks; accepted | new |

### I. Health-data lifecycle and use (M1 to M7)

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T70 | Health data surviving account deletion, or a disconnect that silently keeps upstream access | `owner_id` references `users (id) ON DELETE CASCADE` on every owned table (ADR 0002 §2); system tables with user-linked rows purged (`webhook_events` after 30 days and on deletion, `auth_events`); upstream revoke before the purge (PLAN §4.3); disconnect revokes, deletes the secrets even when a revoke fails and records the failure, and keeps imported data unless the user asks to delete it too (ADR 0004 §7); backups age out (accepted risk 13) | #27: account-deletion test (cascade check, plant-and-delete over every owned and user-linked system table) and disconnect tests | cb T22 |
| T71 | Another household member's measurement attributed to the user (`attrib=1`) | Kept as Withings reports it, excluded from analysis and shown as "unconfirmed" (PLAN §7.1) | M4; accepted risk 14 | new |
| T72 | Misleading health information: a sparse series shown as a trend, estimates shown as certified, experimental interpretations shown to the wrong people | Minimum-data policy, confidence and data quality on every finding; tiers, the experimental tier behind the owner-only flag; estimates never shown as Withings' values (D9, PLAN §7) | M4, M5; M8 medical-claims review | new |
| T73 | Health data sent to a model provider beyond need or without consent | Opt-in flag, explicit consent, minimal tool results, provider review, audit with redacted content, prompt-injection evals; experimental findings never for non-household users; never tokens (PLAN §7.4) | M7 | cb T32 |
| T74 | A stolen session exporting a user's full history | Export requires step-up (PLAN §6) | M7 | new |

## Accepted risks (household phase)

Each holds only while every user is a household member; the M8 gate re-decides all of them.
Items 1, 3, 4, 5, 14 and 17 follow from the specification (§9.2, §4.4, §7.1, §10.2), and item 19
was decided by the Architect (2026-10-31). The others are proposed by this model, most of them
carried over from classbeat's accepted risks; the owner confirms or rejects them before the M2
real-data gate (O15).

1. **Shared home host.** A co-tenant with the Docker socket is root on the host and can read the
   database, the KEK files and every secret (T60). Revisited with the VPS move before M8 (§9.2).
2. **Health data in plaintext at rest.** Only tokens, TOTP seeds and the client secret are
   enveloped; a host or database compromise exposes the health data, and backup encryption
   protects only copies off the host. Disk encryption of the host's volumes is an owner decision
   at the M2 gate (O12); the M8 DPIA re-assesses.
3. **KEKs on the same host as the database.** Envelope encryption protects dumps and backups, not
   a host compromise (ADR 0004). Revisited at the VPS move.
4. **TLS ends at Cloudflare (if proxied) and SWAG**, which see health data and webhook URLs; the
   SWAG → web hop is plain HTTP on the home LAN (T64). Revisited at M8 (processor agreements).
5. **Webhook secrets travel in the URL**, by Withings' design (notifications are unsigned):
   Withings and Cloudflare see them. A leaked secret costs rate budget, never data (T39, T41).
6. **First enrolment race.** Before a new account's first enrolment, and after `admin mfa-reset`
   until the user resets the password, whoever holds the password and enrols first owns the
   second factor; enrolments are mailed (ADR 0003 §2, T18).
7. Someone holding a user's password can exhaust that user's TOTP failure limit (20 per hour)
   and keep them out for that hour (ADR 0003 §2).
8. **Per-account sign-in denial of service** from many IPs; the user signs in from a known device
   (classbeat ADR 0003 §9). Revisited at M8.
9. **No breached-password check** (it needs a new outbound host); mandatory TOTP limits the
   impact. Revisited at M8 (ADR 0003 §10).
10. **The household shares one public IP**, so it shares the per-IP buckets (ADR 0003 §7).
11. A stolen known-device cookie lifts only that user's per-account sign-in cap.
12. **The egress allowlist is in-process**; code running in a process could open a socket around
    it. A network-level egress rule comes with the VPS move.
13. **Backups keep deleted users' data** until they age out; the M8 privacy notice says so (T70).
14. **Withings attributes ambiguous household measurements** (`attrib=1`); ownvitals can't tell
    whose they are (T71).
15. The PKCE verifier and nonce of a Google sign-in flow are stored in plaintext for at most 10
    minutes; they are useless without the one-time code (classbeat).
16. Lost `NOTIFY` wake-ups delay progress by up to 5 s and claims by up to 2 s (ADR 0005).
17. **The merge gate is enforced by agents, not by GitHub** (§10.2, deliberate); correlated model
    errors in review are covered by the deterministic CI security tests (§10.3) and the human
    security review at M8 (PLAN §12).
18. **The operator is trusted.** Someone with host access (the owner) can do anything; the
    controls protect against agents, mistakes and other users, not against the owner's own
    infrastructure.
19. **Stolen Withings access tokens after a KEK compromise.** `keys wipe` does not revoke upstream
    (the tokens it would need are the compromised ones); rotating the Withings client secret makes
    stolen refresh tokens useless, and stolen access tokens stay valid for up to 3 hours (T32,
    ADR 0004 §3).

## Open items

| # | Item | Who, when |
|---|---|---|
| O6 | SWAG: `CF-Connecting-IP` only from Cloudflare's ranges, the webhook location's `access_log` without the URI, no buffering on the SSE path; firewall 8096 from SWAG only (T17, T41) | owner, M2 |
| O7 | Metric labels without user ids or values (T58) | M1 issue |
| O8 | No health data in notification mails (T59) | Architect, M4/M6 |
| O9 | A guard that every fixture is labelled synthetic (`synthetic: true` or a `synthetic_` file prefix) (T67) | M1, with `fake-withings` |
| O12 | Disk encryption on the home server; Cloudflare proxied or DNS-only (T60, T64) | owner, M2 gate |
| O13 | After the port: confirm each M0 mitigation against the merged code, replace every planned test name, close or re-home every open item | #16 |
| O14 | Refuse an unknown webhook secret before any database work (T38) | M1 |
| O15 | Confirm or reject the proposed accepted risks (2, 6 to 13, 15, 16, 18) | owner, before the M2 real-data gate |

Decided by the Architect on 2026-10-31 (numbers kept so earlier references stay valid):

| # | Item | Decision, recorded in |
|---|---|---|
| O1 | Partitioned owned tables (T28) | Allowed under forced RLS on parent and partitions, no app privilege on partitions, one partition function; guard 1 extended by #21 (ADR 0002 §2) |
| O2 | Reconnect and disconnect (T70) | A different Withings account is refused until disconnect; disconnect revokes, deletes the secrets, keeps the row as `disconnected` and the data unless the user asks to delete it; a failed revoke is recorded and retried (ADR 0004 §7) |
| O3 | `webhook_events` retention (T70) | 30 days, then purged by the scheduler; #27's account-deletion test covers it (ADR 0002 §3) |
| O4 | Upstream revoke after a KEK compromise (T32) | Not attempted; the owner rotates the client secret; accepted risk 19 (ADR 0004 §3) |
| O5 | Token exchange in the callback (T36, T48) | One narrow web egress entry for the code exchange only, roadmap Amendment 1 (ADR 0004 §7) |
| O10 | Step-up readings (T6, T7) | A recovery code counts; "disable 2FA" returns the account to enrolment; first enrolment needs no step-up, replacing an authenticator does (ADR 0003 §4) |
| O11 | Session lifetimes (T5) | Idle 72 hours, absolute 30 days, configurable (ADR 0003 §3) |

## Dropped from classbeat's model

- Threats tied to code that is not ported (PLAN §3, D11): MOWL, Swedebeat and Spotify
  connections and writes, songs, playlists, classes, revisions and saves, external operations and
  approval waits, model routes and evals, plans, tiers, quotas, credits, redeem codes and cover art
  (among them classbeat T8 to T11, T13, T14, T23, T25, T29, T43, T44, T47, T51, T52 and T54 to
  T97, except those adapted above).
- classbeat T19 (SSRF through user-supplied links) folds into T48: ownvitals accepts no URL from a
  user.
- AI threats (classbeat T8, T32, T56, T57) return with M7 as T73 and M7's own threat-model update.
