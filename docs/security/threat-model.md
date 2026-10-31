# Threat model: health data

Status: initial M0 version (#4, 2026-10-31), written before the port, with the follow-ups of its
review applied by #49 and those of the #58 review by #72 (2026-10-31). #16 updates it against
the merged code; the human security review of the M8 gate re-checks it end to end. Nothing here
claims a status beyond what D10 allows: no control is INTEGRATION_VERIFIED or PRODUCTION_READY.

## How to read it

- It starts from classbeat's threat model at the pinned commit
  `d80605100992f85f8f02672b9b5f885ed6166214` and re-assesses every ported component for
  **special-category health data** (PLAN §3). The "From" column names the classbeat threat a row
  adapts (`cb T5`) or says `new`. What was dropped is listed at the end.
- **Enforced by** names the issue and the test that enforce a mitigation in M0, using the test
  names the M0 issues port; the M1 issue (#18 to #30, #36) for a Withings mitigation; the
  milestone and PLAN section for one that comes later (`M4: …`); `owner` for an owner action; or
  `accepted` (see "Accepted risks"). #16 replaces every planned test name with the merged one.
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
| Withings app client secret, webhook secrets, the webhook seed and pepper | `withings_apps` (client secret enveloped, webhook secrets as HMACs only), secret files | the client secret mints and revokes tokens; a webhook secret triggers fetches; the seed derives every webhook secret |
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
│  web, operator CLI ── SMTP ──▶ mail relay (never the worker: its mail goes into an outbox
│                                 that web sends; ADR 0003 §6)
│  KEK, webhook seed and pepper files (read-only, outside the database volume; seed on the
│  worker only)

admin's browser ── same path as above ──▶ web admin routes ── ownvitals_maint (BYPASSRLS,
                                          column allowlist, no health table, audited)
owner with host access ── docker compose exec/run ──▶ operator CLI (per-owner contexts);
                          one-off containers ──▶ owner role (`keys wipe`,
                          `withings_apps` configuration)

GitHub ── Actions ── GHCR image + attestation ── forced-command deploy key ──▶ home server
                                                 (the deploy is the boundary: T62)
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
- **The admin path** is a browser like any other, behind TOTP, step-up and the admin role; its
  database role bypasses RLS but reads no health table, and every use is audited (T26, T52,
  T53). **The operator path** needs host access: it is trusted (accepted risk 18), audited where
  it acts on users, and the only way to the owner role (T18, T32).
- **The release path** (amended 2026-10-31, #49): the `release tags` ruleset stops a `v*` tag
  being moved or deleted, not created, and a `v*` tag on a commit outside `main` runs that
  commit's own `release.yml`. So neither the tag nor the workflow's own checks are the boundary:
  **the deploy is**. It validates the tag and digest formats first, resolves the tag's commit
  through `commits/refs/tags/<tag>`, checks that `refs/heads/main` contains it, and verifies the
  attestation against that tag and commit (`--cert-identity`, `--source-ref`,
  `--source-digest`); T62 lists the checks. A tag `creation` rule would add nothing while the
  owner's token is the only pusher (Architect decision). [docs/dev/release.md](../dev/release.md)
  gives the commands; #57 aligns it with T62.

## What changes from classbeat, for health data

- **Impact.** Any confidentiality failure discloses special-category data, usually about another
  household member. Whether GDPR's household exemption (Art. 2(2)(c)) applies depends on the
  nature and scope of the processing, not on where the server sits (§9.3), so it lowers no
  technical bar: the controls are built as if GDPR applied, and the M8 gate assesses rather than
  redesigns.
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
| T75 | Taking over the first admin through `OWNVITALS_ADMIN_EMAILS` with an unverified email | An account exists only with a verified email (Google's address used only when Google marks it verified), so the list grants the role only to an address its holder proved; the grant revokes the user's sessions, and the role counts only with TOTP (ADR 0003 §1, §6) (amended 2026-10-31, #49) | #10 `test_auth_signin.py`, `test_auth_oauth_signin.py` (admin-email grants) | cb T28 |

### B. Tenant isolation ([ADR 0002](../adr/0002-tenant-isolation.md))

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T19 | Cross-tenant read or write of health data (a missing filter, an id from the browser) | `UserContext` everywhere, repository filters, forced RLS with one permissive policy, `UNIQUE (owner_id, id)`, owner-inclusive foreign keys, 404 for foreign ids (ADR 0002 §1, §2) | #5 `test_rls_guard.py`, `test_two_user_isolation.py`, `test_owner_filter.py`, `test_isolation.py`; guard 7 per route from #10 | cb T5 |
| T20 | A webhook, scheduler, worker, admin or CLI context reaching another user's rows | No ambient authority: webhook and scheduler create jobs only inside definer functions that return no owner id; contexts issued only by auth (after TOTP) and from a claimed job row; RLS tests per context (ADR 0002 §4, ADR 0005 §4) | #5 harness; #9 worker; #15 `test_isolation_contexts*.py` and its table-enumerating guard; #24 webhook ingest, #18 scheduler kinds, #30 context tests on every M1 table | cb T5 |
| T21 | Code forging, copying or deserialising a `UserContext` | Issuer token checked by identity; copy, replace, pickle and Pydantic construction refused; AST guard (ADR 0002 §1) | #5 `test_context.py`, `test_user_context_guard.py` | cb ADR 0002 |
| T22 | A pooled connection carrying one user's setting, cursor or state to the next | Transaction-local `set_config`; `user_session` owns the transaction; `DISCARD ALL` on return; checkout check (ADR 0002 §6) | #5 `test_session_guard.py`, `test_db.py` | cb ADR 0002 |
| T23 | Web or worker running with a role that bypasses RLS | Startup and per-connection refusal of superuser, `BYPASSRLS`, dangerous memberships and ownership, no override; the owner password never on web or worker (ADR 0002 §5) | #5 `test_role_check.py`, `test_roles_script.py`, `test_roles_rerun.py` | cb ADR 0002 |
| T24 | SQL injection: the app role can set `app.user_id`, so injected SQL defeats both layers | Bound parameters only, no SQL built from strings (ruff's rule); no `TRUNCATE`, `MAINTAIN` or ownership; role-default timeouts checked at startup and per connection (residual denial of service until detected) (ADR 0002 §5) | #2 ruff settings; #5 guard 2 (`role_guard.py`) and the startup check | cb T26 |
| T25 | Learning or blocking another user's rows through a unique index or foreign key | Owner-inclusive unique indexes and foreign keys; the single exception, `connections (app_id, withings_userid)`, reveals only that a Withings account the caller controls is linked elsewhere (ADR 0002 §2) | #5 guard 1 (`rls_guard.py`); #21 allows the exception by name | cb ADR 0002 |
| T26 | An admin, or a stolen admin session, reading health data | `ownvitals_maint` has no privilege on any health table; column-level allowlist without secret columns; read-only by default; every use audited (ADR 0002 §4) | #12 `maint_guard.py`, `test_maint_privileges.py` | new |
| T27 | `NOTIFY` payloads read by any database listener | Only ids; data read afterwards in a user session (ADR 0005 §7) | #9 `test_jobs_events.py`, `test_queue_functions.py` | cb T30 |
| T28 | A monthly partition of a health table read directly, around its parent's policy, a partition created without RLS, or a partitioned table's key escaping the owner rule | `PRIMARY KEY (owner_id, id, <partition key>)` and never the target of a foreign key, its own foreign keys owner-inclusive ([roadmap Amendment 2](../roadmap.md)); parent and every partition with forced RLS and the `owner_isolation` policy; the app role has privileges on the parent only (`REVOKE ALL` on each partition); partitions created only by the reviewed, pinned `ensure_month_partitions(table, from_month, to_month)` function, which bounds its own range (a fixed 2009-01-01 floor to now + 3 months; past months only as they are written), serializes callers per table with an advisory lock in its own namespace, and is called only by the scheduler module and by every writer of a partitioned table, for the months its rows fall in, in a short transaction of its own; a row outside that range is not written but counted (labels stream and reason only), recorded as skipped and shown in completeness as "skipped (out of range)", never as missing (ADR 0002 §2) (amended 2026-10-31, #49 and #72) | #5 guard 1 refuses partitioned tables until #21; #21 extends guard 1 (key shape, no foreign key into a partitioned table, RLS and privileges on every partition), tests the function and its range bounds, and adds the caller guard; #25 and #26 (the writers' calls, the out-of-range skip) | new |

### C. Keys, tokens and connections ([ADR 0004](../adr/0004-connections-and-encryption.md))

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T29 | Withings tokens read from a database dump or backup | Envelope encryption: a data key per write, AES-256-GCM, AAD naming owner, provider and row, KEK in a file outside the database, a check value against a swapped KEK; backups hold ciphertext (ADR 0004 §2, §3) | #8 `test_envelope.py`, `test_keks.py`; #13 `test_connections.py` (`pg_dump` test) | cb T6, T40 |
| T30 | A token leaking through logs, errors, job rows, events or SSE | Only `ownvitals.connections` decrypts; a redacting credentials object; jobs hold the connection id; `ConnectionFailure` without cause or context; masked log keys (ADR 0004 §5, §8, §9) | #13 `test_connections.py`, `test_connection_errors.py`; #2 `test_logging.py` | cb T41 |
| T31 | One user's tokens used for another user's sync | Owned table read through `UserContext`; credentials rebuilt from the claimed job's owner; moved ciphertext fails its AAD (ADR 0004 §2, §5) | #8 wrong-AAD tests; #13; #15 worker context; #25 two-user sync test with `fake-withings` recording each call's token; #30 context tests | cb T42 |
| T32 | A KEK, or the whole secrets directory, leaked | Everything goes: stop, new KEK, `keys wipe` with the services down (connection rows to `needs_reconnect` without credentials, TOTP reset), older backups treated as compromised; the owner rotates the Withings client secret so stolen refresh tokens are useless; a compromise of the KEK ring is a seed compromise too, so the seed and pepper are rotated and a `compromise` rotation retires every old webhook secret before it adds the new one, and subscriptions without tokens are revoked through `notify list` on reconnect (accepted risk 20); no upstream revoke (stolen access tokens live up to 3 h: accepted risk 19) (ADR 0004 §3, ADR 0008) (amended 2026-10-31, #49 and #72) | #13 `test_keys_wipe.py`; #22 `rotate`, #24 the worker's rotation; owner (runbook, M1) | cb T40 |
| T33 | The current KEK lost | KEKs backed up separately from the dumps; restore drill; without the KEK every user reconnects and re-enrols, by design | owner, M2 (backups, restore drill) | new |
| T34 | A stale refresh, reconnect or 401 overwriting newer tokens or flipping a fresh connection | `token_generation` raised by every token write; token writes and status changes are compare-and-swap on the generation read; rewrap doesn't change it (ADR 0004 §6) | #13 generation CAS test; #22 PLAN §4.3 tests | cb T77 |
| T35 | Losing the only valid refresh token, or using the new access token before the new pair is saved | PLAN §4.3: per-connection lock, durable intent, CAS write, first use after the commit, one recovery retry within the 8-hour grace, then `needs_reconnect`; never the generic retry; a session-scoped advisory lock across the intent commit; reconnect clears a stale intent (ADR 0004 §6, §7) | #22: PLAN §4.3's crash and race tests | new |
| T36 | Withings OAuth abuse: forged state, code injection, a victim's session finishing an attacker's flow, a Withings account already linked to another ownvitals account | State hashed, bound to the session, 10 minutes, single use, taken before the exchange; the code exchanged in the request through web's one narrow egress entry (roadmap Amendment 1); a Withings user linked to another account refused, and a different Withings account refused while one is connected; connect needs recent auth and step-up; `fake-withings` only in tests (ADR 0004 §7) | #13 (step-up on connect); #22 `test_withings_oauth*.py` (state, linked elsewhere, web-only exchange); #27 (a different account refused while connected) | new |
| T37 | The Withings app client secret leaking | Enveloped in `withings_apps` with AAD bound to the app; web (the code exchange) and the worker (refresh, revoke) hold it in memory only for the call; the app role reads it through a column grant; no runtime role (`ownvitals_app`, `ownvitals_maint`) can write the table, and maint reads only its non-secret columns; the configuration is written, and the client secret rewrapped after a KEK rotation, only by #22's tool as the owner role, from one-off operator containers, with an audit row in the same transaction; never logged; rotated by the owner at Withings after any suspected leak (ADR 0002 §3, ADR 0004 §2, §3) (amended 2026-10-31, #49) | #21 (table, grants, guard 11's `withings_apps` check); #22 (`withings_apps` CLI and its rewrap, `test_withings_oauth*.py`); owner (runbook) | new |

### D. The Withings webhook (PLAN §4.4; ADR 0002 §4, ADR 0004 §4, ADR 0005 §8, ADR 0008; amended 2026-10-31, #72)

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T38 | Forged notifications from anyone who knows the URL pattern, a webhook secret recovered from a dump, or an entry planted or garbled in `webhook_secrets` | A 256-bit secret per app in the path, derived from an off-database seed that only the worker holds, never stored; the database keeps only a peppered HMAC with the secret, seed and pepper ids, which web (pepper only) compares in constant time with the pepper the entry names; a retired entry stops verifying at once; no oracle: the request checks run before the secret lookup, and an unknown or retired secret gets the generic 200 from web's in-process copy of the entries, before any database work, and is ignored and counted in a metric (O14); while the copy is older than the entry-cache TTL, or before web has read one at all, every `POST` gets 503, whatever the secret, and Withings retries (`HEAD` stays 200); rotation in the order `rotation_reason` names (ADR 0008): a `compromise` rotation retires every old entry in one atomic call before it appends the new one, and the function refuses an append that would clear a `compromise` request while another entry is not retired, or under a seed a retired entry uses (a guard against an honest worker's mistakes, not against code with the app role: accepted risk 21); the function compare-and-clears the request under its row lock (a routine step against a pending `compromise` is refused as `compromise_pending`, a step whose request changed after the step read it as `stale_request`), and `rotate` never downgrades a pending `compromise` (one conditional `UPDATE`), so a compromise request is never lost; each rotation step runs at READ COMMITTED under an advisory lock per app (namespace 4) and decides from what it read under it, so several workers act on a request one step at a time; a refused step raises an alert, so, with the worker up, a compromised secret doesn't stay verifying, except that code running with the app role could stall a compromise rotation by holding lock 4 or the `withings_apps` row lock: denial of service, surfaced by the stuck-rotation alert (#24) and accepted under risk 21; entries written only through the webhook-secret definer function, owned by the narrow `NOLOGIN` role `ownvitals_webhook_secrets` (no DDL, no other column, no health table), with input rules, an audit row that never holds an `hmac` or anything derived from a secret, and called only by the worker's rotation module (guard), though code running in web could call it too (accepted risk 21); the payload is only a hint: data is always fetched from the API with the user's own token (ADR 0002 §3, ADR 0004 §4, ADR 0008) (amended 2026-10-31, #49 and #72) | #24 webhook fuzzing, forged (PLAN §10.3), rotation in both orders (a retired secret ignored, a compromise request racing a routine append, two workers on one request), no-oracle, stale-cache and no-cache (503) and single-caller-guard tests, the ignored-webhook metric and its alert, READ COMMITTED steps and the stuck-rotation alert; #21 (the function, its `NOLOGIN` role, grants, its lock and isolation check, the input-rule, compare-and-clear, `stale_request`, compromise-order and audit-row tests); #22 (`rotate` never downgrades, under concurrency); #8 (web can't load the seed) | new |
| T39 | Replayed or flooded notifications with a valid (leaked) secret, burning the shared 120/min budget | Dedupe key; singleton jobs with debounce; per-connection and per-app quotas in the ingest function; lanes keep "sync now" first; the worst case is rate budget, never data; rotation with two active secrets | #24 fuzzing (replayed, duplicate) and quotas; #23 601-storm test | new |
| T40 | Oversized or malformed bodies against the parser | `HEAD` → 200; `POST` only form-encoded, over 2 KB refused before parsing, strict fields with sane date bounds | #24 fuzzing (oversized, malformed) | new |
| T41 | The secret leaking through logs or traces, or through `notify list` responses, which carry every subscription's callback URL | Path redacted in app logs, traces and errors; callback URLs, built or listed, never stored, logged or used as metric labels; SWAG's webhook location logs without the URI; log-redaction tests. Cloudflare, if proxied, still sees it (accepted risk 5) (ADR 0004 §9, ADR 0008) (amended 2026-10-31, #49 and #72) | #24 log-redaction test; #22 log-redaction test over `notify list`; owner, M2 (O6) | new |
| T42 | Probing the webhook to learn which Withings users are linked | A generic 200 for an unknown user; internal logging rate-limited | #24 (unknown user, logging) | new |
| T43 | Missed, late, duplicated or out-of-order notifications leaving data incomplete while the UI implies "nothing" or "normal" | Sync cursors, not webhooks, define correctness; poll every 6 h and reconcile nightly; deletions only after two complete reconciliations; per-stream completeness and capability states; missing data never shown as zero; a subscription that can't be re-pointed within 14 days is resubscribed on the next token use (#26) or reconnect (#22), and subscriptions on old callback URLs are revoked through `notify list` on connect and reconnect, found by reconciliation and revoked at disconnect (D3, PLAN §4.2, §4.4; ADR 0008) (amended 2026-10-31, #49 and #72) | #25 (completeness, capability states); #26 (two-pass deletions, drift, the resubscribe after the deadline, old-URL subscriptions); #22 (`notify list` clean-up); #24 (re-pointing deadline); #27 (revoking what `notify list` returns); #30 (PLAN §4.4 checklist) | new |

### E. Jobs, worker and egress ([ADR 0005](../adr/0005-jobs-and-leases.md))

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T44 | The queue's definer functions used to read or move other users' jobs | Three functions owned by `NOLOGIN` `ownvitals_queue`, fixed `search_path`, no caller ids, minimal returns; installed by the superuser script; md5-pinned startup check; no other user code in the database (ADR 0005 §3) | #9 `test_queue_functions.py`, `test_queue_guard.py` (guard 9) | cb T45 |
| T45 | A worker that lost its lease writing job state or health rows | The fencing token checked first in every worker transaction and held to the commit; health rows, revisions and cursors written in fenced transactions (ADR 0005 §4) | #9 `test_jobs_fence.py`, `test_jobs_worker.py`; #25 crash mid-backfill test | cb T12 |
| T46 | A repeated request creating duplicate jobs | `Idempotency-Key` with a request hash, 409 on another body (ADR 0005 §2) | #9 `test_jobs_api.py` | cb T46 |
| T47 | Job events or SSE leaking another user's progress, secrets or health values | Owned events read per request; 404 for foreign ids; payload models allow enums, own ids, counts and dates only (ADR 0005 §7) | #9 `test_jobs_events.py`, `test_jobs_stream.py` | cb T48 |
| T48 | Web or worker reaching arbitrary hosts (SSRF, exfiltration) | One module builds every client; per-process allowlists (worker: Withings hosts, Anthropic only with AI on; web: Google sign-in, the SMTP server for account mails and for the outbox mail that a background task in web sends, and, from M1, only the Withings authorization-code exchange, [roadmap Amendment 1](../roadmap.md)); SMTP from web and the operator CLI through one mail module, never from the worker, whose mail goes into the outbox of ADR 0007, whose rows name a fixed kind and a recipient, never an address or free text, so no row can send mail to an outside address (ADR 0003 §6; amended 2026-10-31, #49 and #72); https, port 443, every redirect hop checked; total deadlines and request budgets; no TLS key log; no URL from user or provider input is fetched | #6 `test_egress.py`, `test_egress_guard.py`, `test_egress_deadline.py`, `test_egress_budget.py`, `test_egress_runtime.py`; #22 (web can't refresh, revoke or call another Withings action); the notifications issue, M6/M7 (outbox rows hold no address or free text; web's list unchanged) | cb T19, T49 |
| T49 | A poison job or retry storm exhausting the shared Withings budget for everyone | Retry budget, capped backoff, non-retryable categories, poison rows failed by the reaper; M1: tokens consumed when claimed and never refunded, 601 pauses the bucket, refresh never retried generically (ADR 0005 §6, §8) | #9 `test_jobs_worker.py`, `test_jobs_errors.py`; #18 retry budget and dead-letter; #23 601-storm and crashed-call tests | cb T50 |
| T50 | A job continuing for a disabled or deleted user | Owner checked after every claim; jobs cascade with the user, so the fence fails on the next write (ADR 0005 §4) | #9 `test_jobs_runner.py` | cb T53 |
| T51 | One user's backfill starving another's sync | Lanes, per-owner fairness, a capped backfill share (ADR 0005 §8) | #23 (lanes, fairness: a backfill can't starve another user's sync); #18 (lanes in the queue) | new |

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
| T57 | Health values, tokens or secrets in logs, errors or traces | Secrets only from files, as `SecretStr`; JSON logs with a redaction filter; no exception text in responses; logs never hold measurement values, ECG samples, tokens, webhook secrets or full webhook paths (AGENTS.md) | #2 `test_logging.py`; #10 `test_auth_logging.py`; #13; #20 and #24 log tests (PLAN §10.3) | cb T7 |
| T58 | Health values or user identifiers exported as Prometheus metrics | Labels limited to app, stream and category; no user id, Withings user id or value in a label | #28 label test (O7) | new |
| T59 | Health data in emails, which pass through a relay and mailboxes outside our control | Notification mails say something is waiting and link to the app; no values, finding text or ECG results; the worker never opens SMTP: its mail goes into the outbox of ADR 0007, whose rows hold only a fixed kind, the recipient (an owned row's `owner_id`, or a system row for an owner notice) and at most a link chosen from fixed routes, never an address, subject, free text, value or finding text; web resolves the address through `ownvitals.auth` when it sends and builds the mail from the kind's fixed template, through the guard-listed mail module; in M1 the owner notice is in-app only (ADR 0003 §6, ADR 0007; amended 2026-10-31, #49 and #72) | M4/M6 (O8: the kinds and templates); the notifications issue (M6/M7) builds the outbox | new |

### H. Hosting, supply chain and agents

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T60 | Compromise of the shared home host (torrent client, flaresolverr, Portainer with the Docker socket) exposing the database, KEKs and secrets | Host firewall (8096 only from SWAG), own Docker network, Postgres unpublished, KEK files read-only outside the database, a deploy key without shell; accepted for the household only (accepted risk 1); EU VPS before M8 (§9.2, D8) | owner, M2; M8 | new |
| T61 | Backup theft or loss | Nightly encrypted `pg_dump`, KEKs backed up separately to another machine; RPO 24 h, RTO 4 h; quarterly restore drill; the real-data gate needs a passing drill (§9.2) | owner, M2 | new |
| T62 | A malicious or tampered image deployed, including one built by a `v*` tag pushed on a commit outside `main` (the `release tags` ruleset stops a tag being moved or deleted, not created, and such a tag runs that commit's own `release.yml`) | `release.yml` attests build provenance; **the deploy is the boundary**: the forced-command key takes a tag and a digest and, in order: (1) validates the tag as strict SemVer (`vMAJOR.MINOR.PATCH`, an optional pre-release, no build metadata) and the digest as `sha256:` plus 64 hex, before using either; (2) resolves the tag's commit through `commits/refs/tags/<tag>`; (3) checks that `refs/heads/main` contains that commit (the compare from it to `refs/heads/main` is `identical` or `ahead`); (4) runs `gh attestation verify` on the digest with `--cert-identity https://github.com/magpern/ownvitals/.github/workflows/release.yml@refs/tags/<tag>`, `--source-ref refs/tags/<tag>`, `--source-digest <commit>` and `--deny-self-hosted-runners`; then it pulls by digest; it has no shell. A tag `creation` rule would add nothing while the owner's token is the only pusher (Architect decision). Deploys only on the owner's go-ahead (§9.2) (amended 2026-10-31, #49) | #7 attestation; #57 aligns [docs/dev/release.md](../dev/release.md) with these checks; owner, M2 deploy script | new |
| T63 | Exposed services or container breakout | Non-root image; local ports on 127.0.0.1; Postgres never published; only SWAG reaches web | #2 compose; #14 stack check (non-root, no secret in `docker inspect`) | cb T21 |
| T64 | Cloudflare and SWAG seeing plaintext (pages, API responses, webhook URLs); plain HTTP from SWAG to web on the LAN | SSL Full (strict); firewall; accepted for the household (accepted risk 4); Cloudflare is a processor question at M8 (§9.3) | owner, M2 (O12); M8 | new |
| T65 | A compromised dependency, base image or Action | Lockfiles, exact versions, SHA-pinned Actions, checksum-verified tools, Dependabot, least-privilege `permissions`, `persist-credentials: false`; Trivy image scan | #1 CI; #7 Dependabot; #14 Trivy | cb T17 |
| T66 | An agent committing a secret or weakening a check | gitleaks on each PR's commits and weekly on the history; frozen-spec guard; the review gate; the ruleset's required checks | #1 `ci` / `checks` | cb T18 |
| T67 | Real health data reaching agents, fixtures, tests, logs, PRs or issues | No real data on the development machine; synthetic, labelled fixtures; `fake-withings` and the demo account only; the probe prints shapes only and the owner runs it; agents never connect a real account (AGENTS.md, §10.4) | #1 AGENTS.md and review checks; #29 probe tests (shapes only); #19 synthetic fixtures (O9) | new |
| T68 | Prompt injection of the development agents through issues, PRs or upstream text | Repository and web content treated as data; the review gate and ruleset on every merge; no secret in any prompt | #1 AGENTS.md, `.claude/agents/` | cb T31 |
| T69 | The public repository telling an attacker the design | No control relies on secrecy of the design; secrets never in the repository | #1 gitleaks; accepted | new |

### I. Health-data lifecycle and use (M1 to M7)

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T70 | Health data surviving account deletion, or a disconnect that silently keeps upstream access | `owner_id` references `users (id) ON DELETE CASCADE` on every owned table (ADR 0002 §2); system tables with user-linked rows purged (`webhook_events` after 30 days and on deletion, `auth_events`); upstream revoke before the purge (PLAN §4.3); disconnect revokes every subscription `notify list` returns (old URLs included), deletes the secrets even when a revoke fails and records the failure, and keeps imported data unless the user asks to delete it too (ADR 0004 §7); backups age out (accepted risk 13) (amended 2026-10-31, #49) | #27: account-deletion test (cascade check, plant-and-delete over every owned and user-linked system table) and disconnect tests | cb T22 |
| T71 | Another household member's measurement attributed to the user (`attrib=1`) | Kept as Withings reports it, excluded from analysis and shown as "unconfirmed" (PLAN §7.1) | M4 (PLAN §7.1); accepted risk 14 | new |
| T72 | Misleading health information: a sparse series shown as a trend, estimates shown as certified, experimental interpretations shown to the wrong people | Minimum-data policy, confidence and data quality on every finding; tiers, the experimental tier behind the owner-only flag; estimates never shown as Withings' values (D9, PLAN §7) | M4, M5 (PLAN §7); M8 medical-claims review | new |
| T73 | Health data sent to a model provider beyond need or without consent | Opt-in flag, explicit consent, minimal tool results, provider review, audit with redacted content, prompt-injection evals; experimental findings never for non-household users; never tokens (PLAN §7.4) | M7 (PLAN §7.4) | cb T32 |
| T74 | A stolen session exporting a user's full history | Export requires step-up (PLAN §6) | M7 (PLAN §6) | new |

## Accepted risks (household phase)

Each holds only while every user is a household member; the M8 gate re-decides all of them.
Items 1, 3, 4, 5, 14 and 17 follow from the specification (§9.2, §4.4, §7.1, §10.2). Items 19
to 21 follow from Architect decisions (2026-10-31; #49 extended 20 and 21; #72 scoped 21's
input rules and added three ways to stall a rotation to it); the others are proposed by this
model, most of them carried over from classbeat's accepted risks. The owner confirms or rejects
every item that doesn't come from the specification before the M2 real-data gate (O15).

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
20. **Old webhook URLs after a seed compromise** (or a compromise of the KEK ring that holds the
    seed; amended 2026-10-31, #49). A `compromise` rotation retires the old secret first,
    before it adds the new one, so notifications to the old URL get the generic 200 and are
    counted and ignored. Two kinds of subscription keep that URL for a while:
    - those whose access tokens the wipe removed can't be re-pointed: they keep it until the user
      reconnects, when the worker revokes them through `notify list` and subscribes with the new
      secret; webhook-driven sync stops for those users until then;
    - those still failing at the 14-day re-pointing deadline keep it until they are
      resubscribed (#26 on the next successful token use, or #22 on reconnect); scheduled sync
      covers them meanwhile; a disconnect revokes them through `notify list` (#27).

    A compromise rotation takes the URLs it re-points from `notify list` and cancels the
    queued jobs of a routine rotation it replaces; the old seed and pepper go at the end of its
    order, and since they are already known, keeping them until then adds no exposure.
    Payloads are only hints,
    and after reconnect the backfill, poll and reconciliation bring the data up to date (D3)
    (T32, T38, ADR 0004 §3, ADR 0008; amended 2026-10-31, #72).
21. **Code running in web could plant, retire or garble webhook-secret entries, or stall a
    rotation** (amended 2026-10-31, #49 and #72). Web shares `ownvitals_app` with the worker,
    so code execution in web could call the webhook-secret function, within its input rules,
    which bound shape, not intent. The known-id rule and the compromise-order rules
    (`unretired_entries`, `seed_reused`) check only the ids the caller names, and the app role
    can insert `connection_keks` rows itself, so they guard against an honest worker's
    mistakes, not against this code (ADR 0004 §4; amended 2026-10-31, #72). It could call the
    function:
    - to add an entry whose HMAC it chose, under a seed id it inserted itself if it likes,
      giving it a webhook URL it can call. Code in web sees the paths it serves, so that can be
      a URL a compromise rotation retired, which then verifies again (#72). Payloads are only a
      trigger to fetch with the user's own token (PLAN §4.4), so the worst case is extra syncs
      within the rate budget;
    - to retire entries, delete retired ones or re-hash entries to garbage (denial of service,
      extended by #49): webhooks are then ignored, and the worker's derivation check refuses to
      build callback URLs from a garbled entry, until a rotation replaces it. Scheduled sync and
      drift reconciliation (#26) cover the gap, and an alert fires on the ignored-webhook rate.

    It could also stall a rotation (amended 2026-10-31, #72):
    - by holding the rotation lock (namespace 4, ADR 0008 §2): the worker's steps then fail on
      `lock_timeout` and alert;
    - by calling the function and keeping its transaction open, which holds the `withings_apps`
      row lock and blocks `rotate` too; the 60 s idle-in-transaction timeout ends it, but it
      can repeat it;
    - by calling the queue's definer function that cancels a routine rotation's queued
      `notify update` jobs (ADR 0008; #18 builds it), which stalls a routine rotation.

    Each is denial of service, never a secret disclosure, and surfaces through the
    stuck-rotation alert (#24). While a `compromise` request is stalled, the old secret keeps
    verifying; that gives this code nothing more, since code running in web already decides how
    webhooks are answered.

    The function's own role holds no DDL right, no other column and no health table (ADR 0002
    §3), so a bug in it reaches nothing more; the guard keeps every module but the worker's
    rotation from calling it (T38, ADR 0004 §4).

## Open items

| # | Item | Who, when |
|---|---|---|
| O6 | SWAG: `CF-Connecting-IP` only from Cloudflare's ranges, the webhook location's `access_log` without the URI, no buffering on the SSE path; firewall 8096 from SWAG only (T17, T41) | owner, M2 |
| O7 | Metric labels without user ids or values (T58) | #28 (its acceptance criterion) |
| O8 | No health data in notification mails: the kinds and their templates (T59; what an outbox row may hold is fixed by ADR 0007; amended 2026-10-31, #72) | Architect, M4/M6 |
| O9 | A guard that every fixture is labelled synthetic (`synthetic: true` or a `synthetic_` file prefix) (T67) | #19, with `fake-withings` |
| O12 | Disk encryption on the home server; Cloudflare proxied or DNS-only (T60, T64) | owner, M2 gate |
| O13 | After the port: confirm each M0 mitigation against the merged code, replace every planned test name, close or re-home every open item | #16 |
| O14 | Answer an unknown or retired webhook secret with the generic 200 before any database work, ignore it and count it; run the request checks before the lookup; answer a `POST` with 503 while the entry cache is stale or not yet read, and `HEAD` with 200 (T38; amended 2026-10-31, #49 and #72) | #24 |
| O15 | Confirm or reject the accepted risks that don't come from the specification (2, 6 to 13, 15, 16, 18 to 21; #49 extended 20 and 21, 21 now covering denial of service; #72 added to 21 the three ways to stall a rotation (the rotation lock, an open transaction holding the `withings_apps` row lock, the cross-user cancel) and said its input rules guard against an honest worker's mistakes only; amended 2026-10-31, #72) | owner, before the M2 real-data gate |

Decided by the Architect on 2026-10-31 (numbers kept so earlier references stay valid):

| # | Item | Decision, recorded in |
|---|---|---|
| O1 | Partitioned owned tables (T28) | Allowed with `PRIMARY KEY (owner_id, id, <partition key>)` and no foreign key into them ([roadmap Amendment 2](../roadmap.md)), forced RLS on parent and partitions, no app privilege on partitions, one partition function; guard 1 extended by #21 (ADR 0002 §2) |
| O2 | Reconnect and disconnect (T70) | A different Withings account is refused until disconnect; disconnect revokes, deletes the secrets, keeps the row as `disconnected` and the data unless the user asks to delete it; a failed revoke is recorded and retried (ADR 0004 §7) |
| O3 | `webhook_events` retention (T70) | 30 days, then purged by the scheduler; #27's account-deletion test covers it (ADR 0002 §3) |
| O4 | Upstream revoke after a KEK compromise (T32) | Not attempted; the owner rotates the client secret; accepted risk 19, for the owner to confirm (O15) (ADR 0004 §3) |
| O5 | Token exchange in the callback (T36, T48) | One narrow web egress entry for the code exchange only, [roadmap Amendment 1](../roadmap.md) (ADR 0004 §7) |
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
