# Threat model: health data

Status: initial M0 version (#4, 2026-10-31), written before the port, with the follow-ups of its
review applied by #49, those of the #58 review by #72, those of the #73 review by #78 and those of
the #83 review by #88 (2026-10-31). **Post-port review by #16 (2026-11-03)** against `main` at
`5efe73c`: every component of PLAN §3's M0 rows re-assessed for health data ("Post-port review"
below), every planned M0 test name replaced by the merged test, and the M1 rows linked to the
merged M1 tests as far as a file and test name show them (#30 maps the PLAN §4.4 checklist test by
test). The human security review of the M8 gate re-checks it end to end. Nothing here claims a
status beyond what D10 allows: synthetic tests make a control IMPLEMENTED at most; no control is
INTEGRATION_VERIFIED or PRODUCTION_READY.

## How to read it

- It starts from classbeat's threat model at the pinned commit
  `d80605100992f85f8f02672b9b5f885ed6166214` and re-assesses every ported component for
  **special-category health data** (PLAN §3). The "From" column names the classbeat threat a row
  adapts (`cb T5`) or says `new`. What was dropped is listed at the end.
- **Enforced by** names the issue and the test that enforce a mitigation: a **linked** name
  (`[test_x.py]`, defined under "Test files" at the end) is a merged test file, checked to exist
  by #16, with the load-bearing test functions in backticks after it; a test file in backticks only
  (`test_passkey_signin.py`) is one a later issue plans, or a merged one it will extend (sections
  J and K). Otherwise the
  milestone and PLAN section for a mitigation that comes later (`M4: …`); `owner` for an owner
  action; or `accepted` (see "Accepted risks"). **Bold** text in the column is a gap the review
  found ("Gaps found by the post-port review"), with its owner.
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
  gives the commands; #57 aligned it with T62.

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

## Post-port review (#16)

PLAN §3 asks for a health-data review of every component ported in M0. The table names each
component of §3's M0 rows as merged, what the port changed for health data, and the threats
(below) that carry its mitigations and tests. The rows from M7 (`agent/providers/anthropic.py`,
`evals/*`, `quota/ledger.py`, `agent/prices.py`) are section K's; the not-ported row is under
"Dropped from classbeat's model".

| classbeat (PLAN §3) | ownvitals, as merged | Reviewed for health data | Threats |
|---|---|---|---|
| `auth/*`, ADR 0003 | `ownvitals.auth` (#10, #11; #140, #143, #146 follow-ups) | A password alone reaches nothing: mandatory TOTP and the second-factor gate on every route, enumerated by a test; step-up on connect, disconnect, delete and credential changes; TOTP seeds enveloped; account tables only through `ownvitals.auth` (guard 8, [test_auth_tables_guard.py]); no health value in auth events or mail | T1 to T18, T75 |
| `crypto/envelope.py`, `keks.py`, `keys_cli.py`, `connections/rewrap.py`, ADR 0004 | `ownvitals.crypto`, `ownvitals.keys_cli` (#8, #13) | Tokens, seeds, the pepper and the client secret enveloped with owner-bound AAD; web never holds the webhook seed; `keys wipe` and `rewrap` resumable; the envelope reachable only from listed packages ([test_crypto_guard.py]). Health data itself is not enveloped (accepted risk 2) | T8, T29, T32, T33, T37 |
| `connections/*` | `ownvitals.connections` (#13), `ownvitals.withings` (#20 to #27) | Spotify gone; one live row per provider, token generations with compare-and-swap, crash-safe refresh (PLAN §4.3); failures carry a category and nothing else | T30, T31, T34 to T37 |
| `context.py`, `db/rls.py`, `role_check.py`, ADR 0002, AST guards | `ownvitals.context`, `ownvitals.db` (#5; #15; #75, #89) | Every owned table under forced RLS, checked in every execution context (web, worker, scheduler, webhook, maintenance, admin) with an enumerating guard; two issuers only; the webhook and scheduler create jobs through definer functions that return no owner id | T19 to T28 |
| `jobs/*`, `worker/*`, ADR 0005 | `ownvitals.jobs`, `ownvitals.worker` (#9, #18, #164) | Job inputs and events hold ids, enums and counts only; `NOTIFY` carries ids; the fence on every write; the owner re-checked after each claim | T27, T44 to T47, T49 to T51 |
| `admin/audit*`, `flags/*`, `log.py`, `headers.py`, `auth/clientip.py` | `ownvitals.admin` (#12, #127), `ownvitals.flags`, `ownvitals.log`, `ownvitals.headers`, `ownvitals.auth.clientip` (#2, #10) | `ownvitals_maint` reads no health table (classbeat's maint read content); audit rows hold keys, never values; the experimental tier and AI default off; log redaction covers webhook paths, OAuth codes and cookies; `CF-Connecting-IP` only from listed proxies | T17, T26, T52 to T58 |
| `egress.py` (M0 part) | `ownvitals.egress` (#6, #122, ADR 0009), `ownvitals.mail` | Allowlists per process role, enforced statically and at run time; the worker never opens SMTP; web's one Withings call is the code exchange (roadmap Amendment 1) | T48, T59 |
| `e2e/fake-oidc`, CI, commitlint, Dependabot, CODEOWNERS, PR template, rulesets, release | `e2e/` (#14), `.github/` (#1, #7, #57), `deploy/` (#84, #101) | Synthetic identities only; `fake-oidc` only in the e2e overlay; image and repository scans; the deploy is the release boundary | T2, T13, T62 to T66 |
| `AGENTS.md`, `CLAUDE.md`, `.claude/agents/*`, ADR, status, runbook and threat-model formats | the same files (#1, #4) | No real health data for agents; status levels; the review gate | T66 to T68 |

### Gaps found by the post-port review

Each gap is a mitigation the code implements, or claims, that no merged test enforces, or a
mitigation not yet built. None blocks M0's "done when"; each has an owner.

| # | Gap | Threat | Owner |
|---|---|---|---|
| G1 | No test sends a hostile `Host` header to a flow that mails a link; the links come only from `OWNVITALS_PUBLIC_BASE_URL` in `auth/mails.py`, but nothing pins that | T12 | #165 |
| G2 | `fake-oidc`'s refusal to start without `OWNVITALS_ENVIRONMENT=test` (`e2e/fake-oidc/server.mjs`) and the absence of the e2e CA from the release image are untested | T13 | #165 |
| G3 | The rate limit on the receiver's unknown-user log line (`withings/webhooks.py`, `_note_unknown`) is untested | T42 | #165 |
| G4 | Only `backend/tests/fixtures/withings/` is checked for the synthetic label; no repository-wide guard, and `backend/tests/ecg/golden_estimated.json` (expected outputs of synthetic strips) carries neither the `synthetic_` prefix nor `synthetic: true` | T67, O9 | #165 |
| G5 | Guard 7 is per route (ADR 0002 §7): each id-bearing route merged so far has its foreign-id 404 test, but nothing enumerates the routes, so a new one without the test isn't caught | T19 | #165 |
| G6 | The production `compose.yaml` on the home server is not in the repository, so no test checks that it publishes no Postgres port; the repository's local stack publishes Postgres on 127.0.0.1 for development | T63 | owner, M2 (O6) |

Already tracked in #165 and named in the rows: the missing per-connection and per-app webhook
quotas (T39), the Withings OAuth state kept in the web process rather than a table (T36), a
database error after a secret match answering 500 (T38), admin audit rows written in a later
transaction (T53), and the unwritten `resubscribe` mark (T43).

## Threats and mitigations

### A. Accounts, sessions and step-up ([ADR 0003](../adr/0003-accounts-and-sessions.md))

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T1 | Takeover by matching an attacker's Google account to the victim's email | Identity key `(google, sub)`, never the email; a verified Google email matching an existing user creates and links nothing; linking needs a signed-in session, recent auth and step-up (ADR 0003 §1, §4) | #10 [test_auth_oauth_signin.py] (identity by `sub`, the email-collision outcomes), [test_auth_oauth_link.py] (`test_an_identity_on_another_user_is_refused`, `test_link_needs_recent_authentication_before_the_start`) | cb T1 |
| T2 | Login CSRF, OAuth state or code injection at sign-in | State, PKCE (S256) and nonce in a 10-minute flow row taken atomically before the code exchange; the callback bound to the browser's flow cookie; no sign-in from a mail link (ADR 0003 §1) | #10 [test_auth_oauth_signin.py], [test_auth_oauth_pkce.py], [test_auth_csrf.py]; #14 e2e [auth-google.spec.ts] (state, nonce and PKCE checked by `fake-oidc`) | cb T2 |
| T3 | Credential stuffing and password brute force | argon2id; failure-only limits per (account, IP), per account and per IP, reserved before the check; no lockout; invite-only sign-up; a correct password alone still reaches no data (T4) | #10 [test_auth_ratelimit.py], [test_auth_signin.py]; #11 [test_auth_mandatory_totp.py] | cb T3 |
| T4 | A phished or reused password reaching health data | TOTP mandatory for every account; a session without a verified second factor never yields a `UserContext` and reaches only the verify/enrol allowlist; it is deleted after 10 minutes; a test enumerates every route; the SPA's guards are convenience, the API enforces (ADR 0003 §2) | #11 [test_auth_mandatory_totp.py] (`test_every_route_but_the_allowlist_refuses_a_session_without_its_second_factor` enumerates every registered route); #3 [guards.test.tsx] (the SPA's guards); #14 e2e [auth-password.spec.ts], [auth-google.spec.ts] (sign-up → forced enrolment → sign-in with a code) | new |
| T5 | Session theft or fixation | Sessions server-side, stored as SHA-256; `__Host-` cookie, `HttpOnly`, `Secure`, `SameSite=Lax`; rotation on sign-in, second factor, re-auth, step-up and privilege change; idle and absolute limits; revocation rules; Origin check and CSRF token on writes (ADR 0003 §1, §3) | #10 [test_auth_sessions.py], [test_auth_csrf.py]; #11 [test_auth_sessions_protection.py]; #140 [test_session_window.py] | cb T4 |
| T6 | A stolen live session (shared household computer, unlocked laptop) used to connect or disconnect Withings, delete the account, change email or password, swap the second factor or log the owner out | Step-up, a TOTP or recovery code within 10 minutes, for PLAN §6's list, plus recent authentication where classbeat has it; refusals `step_up_required`, audited (ADR 0003 §4) | #11 [test_auth_stepup.py] (one test per action of PLAN §6), [test_admin_stepup_window.py]; #13 [test_connections.py] (`test_connect_and_disconnect_require_step_up`); #22 [test_withings_oauth.py] (`test_start_requires_step_up`); #27/#163 [test_withings_disconnect.py] (`test_disconnect_requires_step_up`), [test_withings_disconnect_account.py] (`test_account_delete_requires_step_up`), [test_withings_disconnect_routes.py] (`test_the_routes_need_a_step_up_and_enqueue_nothing_without_it`); #14 e2e [step-up.spec.ts] | cb T27 |
| T7 | Guessing or replaying a TOTP or recovery code, at sign-in or at step-up | Replay refused by the conditional `last_step` update; recovery codes single-use; MFA failure buckets (5 per 5 min per account and IP, 20 per hour per account); failures audited, five in an hour mailed (ADR 0003 §1, §2) | #10 [test_auth_mfa_verify.py], [test_auth_mfa_hardening.py], [test_auth_ratelimit.py]; #11 [test_auth_stepup.py]; #14 e2e [step-up.spec.ts] (a wrong code changes nothing) | new |
| T8 | TOTP seeds or recovery codes read from a dump or backup | Seeds under the envelope (AAD bound to the user, KEK outside the database); recovery codes as HMACs (ADR 0003 §5, ADR 0004 §2) | #8 [test_envelope.py], [test_envelope_rules.py]; #10 [test_totp_envelope.py], [test_auth_mfa.py] (recovery codes stored as hashes only); #13 [test_keys_rewrap.py] | cb T35 |
| T9 | Account enumeration through sign-in, sign-up, reset or timing | One message per flow; "check your inbox" either way; a dummy argon2 verification; mail sent after the response (ADR 0003 §1) | #10 [test_auth_signin.py] (the dummy verification), [test_auth_signup.py] (`test_an_existing_address_gets_the_same_answer_and_a_different_mail`), [test_auth_reset.py] | cb T33 |
| T10 | A reset or verification link leaking or reused | 32 random bytes, SHA-256 stored, single use, short-lived, in the URL fragment; a reset revokes every session; mail over TLS except to loopback or mailpit | #10 [test_auth_reset.py], [test_auth_email.py], [test_auth_signup.py] (links in the fragment, single use); #6 [test_mail.py] (`test_plain_smtp_is_refused_for_other_hosts`), [test_config_milestone.py] | cb T34 |
| T11 | Open redirect after sign-in | `return_to` only as a relative path, checked again before use | #10 [test_auth_oauth_signin.py] (`return_to`) | cb T36 |
| T12 | Host-header poisoning of links in mail | Links built only from `OWNVITALS_PUBLIC_BASE_URL` | Links are built only in `auth/mails.py` from the setting, never from the request. [test_auth_signup.py] checks the mailed link's origin; [test_auth_reset.py] and [test_auth_email.py] check only its path and fragment; and the test client's base URL equals the configured origin, so no test tells the setting apart from the request's host. **No test sends a hostile `Host` header** (gap G1, #165) | cb T37 |
| T13 | A forged Google ID token, or a test fake reachable in production | RS256 only against Google's JWKS; `iss`, `aud`/`azp`, `exp`, `iat`, `nonce` checked; one provider and callback path; `fake-oidc` configurable only with `OWNVITALS_ENVIRONMENT=test` (ADR 0003 §1) | #10 [test_oauth_idtoken.py]; #6 [test_egress_runtime.py] (Google hosts are code, `test_the_google_oauth_client_refuses_a_redirect_to_another_host`); #14: `fake-oidc` exists only in `compose.e2e.yaml` and its `server.mjs` refuses to start unless `OWNVITALS_ENVIRONMENT=test`; the e2e CA is mounted over web's bundle by the overlay only. **The refusal and the absence of the test CA in the image are untested** (gap G2, #165) | cb T38 |
| T14 | Account pre-hijacking | No unverified accounts; password typed again at confirmation; a provider sign-up with an unverified email confirms only in its own browser; every link mailed (ADR 0003 §1) | #10 [test_auth_signup.py], [test_auth_oauth_handoff.py] | cb T39 |
| T15 | Email abuse: sign-up spam, reset flooding | Invite codes; per-address and per-IP mail limits | #10 [test_auth_signup.py] (`test_mail_limits_per_address_and_per_ip`, the invite tests), [test_auth_ratelimit.py] | cb T24 |
| T16 | Sign-up opened beyond the household before the M8 gate (D7) | `open` refused by setting validation; the stored flag can only tighten (ADR 0003 §6) | #10 [test_flags.py], [test_auth_signup.py] (`test_open_signups_are_refused_until_the_m8_gate`); #12 [test_flags_db.py], [test_admin_settings.py] | new |
| T17 | A client choosing its own IP to escape or poison rate-limit buckets | Peer address unless the peer is a trusted proxy (SWAG); `CF-Connecting-IP` only from SWAG; port 8096 open only to SWAG; SWAG sets that header only for Cloudflare's ranges (ADR 0003 §8) | #10 [test_auth_clientip.py], [test_auth_ratelimit.py] (client-IP cases); owner, M2 (O6) | new |
| T18 | A lost authenticator locking a user out, or the recovery path used for a takeover | Recovery codes; `admin mfa-reset` only from the CLI (host access), audited, revokes sessions, mails a reset link; first enrolment race accepted (accepted risk 6) (ADR 0003 §2) | #10 [test_auth_cli.py] (`test_mfa_reset_removes_totp_and_recovery_codes_revokes_sessions_and_mails_a_link`), [test_auth_mfa.py] | new |
| T75 | Taking over the first admin through `OWNVITALS_ADMIN_EMAILS` with an unverified email | An account exists only with a verified email (Google's address used only when Google marks it verified), so the list grants the role only to an address its holder proved; the grant revokes the user's sessions, and the role counts only with TOTP (ADR 0003 §1, §6) (amended 2026-10-31, #49) | #10 [test_auth_signin.py], [test_auth_oauth_signin.py] (admin-email grants), [test_auth_sessions_protection.py] (the grant revokes sessions) | cb T28 |

### B. Tenant isolation ([ADR 0002](../adr/0002-tenant-isolation.md))

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T19 | Cross-tenant read or write of health data (a missing filter, an id from the browser) | `UserContext` everywhere, repository filters, forced RLS with one permissive policy, `UNIQUE (owner_id, id)`, owner-inclusive foreign keys, 404 for foreign ids (ADR 0002 §1, §2) | #5 [test_rls_guard.py] (guard 1), [test_isolation.py] (guard 3), [test_two_user_isolation.py], [test_owner_filter.py]; #15 [test_isolation_contexts_web.py]; guard 7 per route (ADR 0002 §7): [test_jobs_api.py] (`test_another_users_job_answers_exactly_like_an_unknown_id`), [test_jobs_stream.py] (`test_foreign_unknown_and_malformed_ids_are_the_same_404`), [test_auth_sessions.py] (`test_another_users_session_id_is_a_404_like_an_unknown_one`), [test_auth_oauth_link.py] (`test_unlinking_another_users_or_an_unknown_identity_is_a_404`) | cb T5 |
| T20 | A webhook, scheduler, worker, admin or CLI context reaching another user's rows | No ambient authority: webhook and scheduler create jobs only inside definer functions that return no owner id; contexts issued only by auth (after TOTP) and from a claimed job row; RLS tests per context (ADR 0002 §4, ADR 0005 §4) | #15 [test_isolation_contexts_web.py], [test_isolation_contexts_worker.py], [test_isolation_contexts_scheduler.py], [test_isolation_contexts_webhook.py], [test_isolation_contexts_maintenance.py], [test_isolation_contexts_admin.py] and the table-enumerating guard [test_isolation_contexts_guard.py] (every owned table, M1's included, needs its row); #9 [test_jobs_fence.py] (the job issuer); #164 [test_queue_schedule.py] (the scheduler's definer function, counts only); #21 [test_withings_schema.py] (`test_webhook_events_are_writable_only_through_the_definer`) | cb T5 |
| T21 | Code forging, copying or deserialising a `UserContext` | Issuer token checked by identity; copy, replace, pickle and Pydantic construction refused; AST guard (ADR 0002 §1) | #5 [test_context.py], [test_user_context_guard.py] (guard 5) and its continuations ([test_user_context_writers.py], [test_user_context_routes.py], [test_user_context_types.py] and the other `test_user_context_*.py`) | cb ADR 0002 |
| T22 | A pooled connection carrying one user's setting, cursor or state to the next | Transaction-local `set_config`; `user_session` owns the transaction; `DISCARD ALL` on return; checkout check (ADR 0002 §6) | #5 [test_session_guard.py], [test_isolation.py] (guard 4: the pool reset and the checkout check), [test_db.py] | cb ADR 0002 |
| T23 | Web or worker running with a role that bypasses RLS | Startup and per-connection refusal of superuser, `BYPASSRLS`, dangerous memberships and ownership, no override; the owner password never on web or worker (ADR 0002 §5) | #5 [test_role_check.py], [test_role_check_login.py], [test_role_check_refused.py] (guard 6), [test_role_guard.py] (guard 2), [test_roles_script.py], [test_roles_rerun.py]; #9 [test_compose_roles.py] | cb ADR 0002 |
| T24 | SQL injection: the app role can set `app.user_id`, so injected SQL defeats both layers | Bound parameters only, no SQL built from strings (ruff's rule); no `TRUNCATE`, `MAINTAIN` or ownership; role-default timeouts checked at startup and per connection (residual denial of service until detected) (ADR 0002 §5) | #2 ruff's `S` rules (`backend/pyproject.toml`, so S608 string-built SQL fails lint); #5 [test_role_guard.py] (guard 2: no `TRUNCATE`, `MAINTAIN` or ownership); #77 [test_role_check.py] (`test_transaction_timeout_unset_or_above_the_setting_is_refused` and the other role-default timeouts) | cb T26 |
| T25 | Learning or blocking another user's rows through a unique index or foreign key | Owner-inclusive unique indexes and foreign keys; the single exception, `connections (app_id, withings_userid)`, reveals only that a Withings account the caller controls is linked elsewhere (ADR 0002 §2) | #5 [test_rls_guard.py] (`test_only_the_registered_global_unique_index_may_leave_out_owner_id`); #21 [test_withings_schema.py] (`test_linking_the_same_withings_account_twice_is_refused`) | cb ADR 0002 |
| T26 | An admin, or a stolen admin session, reading health data | `ownvitals_maint` has no privilege on any health table; column-level allowlist without secret columns; read-only by default; every use audited (ADR 0002 §4) | #12 [test_maint_privileges.py] (guard 11), [test_admin_db.py], [test_admin_jobs.py] (aggregates only); #15 [test_isolation_contexts_admin.py] (the grant matrix) | new |
| T27 | `NOTIFY` payloads read by any database listener | Only ids; data read afterwards in a user session (ADR 0005 §7) | #9 [test_jobs_events.py] (`pg_notify` payloads), [test_queue_functions.py] | cb T30 |
| T28 | A monthly partition of a health table read directly, around its parent's policy, a partition created without RLS, or a partitioned table's key escaping the owner rule | `PRIMARY KEY (owner_id, id, <partition key>)` and never the target of a foreign key, its own foreign keys owner-inclusive ([roadmap Amendment 2](../roadmap.md)); parent and every partition with forced RLS and the `owner_isolation` policy; the app role has privileges on the parent only (`REVOKE ALL` on each partition); partitions created only by the reviewed, pinned `ensure_month_partitions(table, from_month, to_month)` function, which bounds its own range (a fixed 2009-01-01 floor to now + 3 months; past months only as they are written), serializes callers per table with an advisory lock in its own namespace, and is called only by the scheduler module and by every writer of a partitioned table, for the months its rows fall in, in a short transaction of its own; a row outside that range is not written but counted (labels stream and reason only), recorded as skipped and shown in completeness as "skipped (out of range)", never as missing (ADR 0002 §2) (amended 2026-10-31, #49 and #72) | #21 [test_rls_guard.py] (guard 1's partition rules), [test_withings_schema.py] (`test_only_listed_modules_name_the_partition_function`: the caller guard), [test_withings_schema_functions.py] (`test_a_month_outside_the_floor_or_the_ceiling_is_refused`, `test_the_app_role_reaches_a_partition_only_through_its_parent`); #25/#26 [test_ingest_revisions.py] (the writers' calls, the out-of-range skip), [test_withings_sync.py] (`test_out_of_range_series_and_devices_move_the_skip_count`); #15 [test_isolation_contexts_guard.py] (a partitioned parent in the mutation matrix) | new |

### C. Keys, tokens and connections ([ADR 0004](../adr/0004-connections-and-encryption.md))

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T29 | Withings tokens read from a database dump or backup | Envelope encryption: a data key per write, AES-256-GCM, AAD naming owner, provider and row, KEK in a file outside the database, a check value against a swapped KEK; backups hold ciphertext (ADR 0004 §2, §3) | #8 [test_envelope.py], [test_envelope_rules.py], [test_keks.py], [test_keks_files.py], [test_keks_check.py]; #13 [test_connections.py] (`test_credentials_are_not_stored_or_logged_and_a_wrong_aad_fails` reads the stored columns as the database holds them, which is what a dump or backup holds; there is no separate `pg_dump` run); [test_crypto_guard.py] | cb T6, T40 |
| T30 | A token leaking through logs, errors, job rows, events or SSE | Only `ownvitals.connections` decrypts; a redacting credentials object; jobs hold the connection id; `ConnectionFailure` without cause or context; masked log keys (ADR 0004 §5, §8, §9) | #13 [test_connections.py], [test_connection_errors.py]; #2 [test_logging.py], [test_log_redaction.py], [test_log_fail_closed.py], [test_log_chain.py]; #20 [test_withings_client_logs.py] | cb T41 |
| T31 | One user's tokens used for another user's sync | Owned table read through `UserContext`; credentials rebuilt from the claimed job's owner; moved ciphertext fails its AAD (ADR 0004 §2, §5) | #8 wrong-AAD tests ([test_envelope_rules.py]); #13 [test_connections.py]; #15 [test_isolation_contexts_worker.py]; #25 [test_withings_sync.py]; #164 [test_worker_withings_e2e.py]; #30 maps PLAN §4.4's two-user item to its named test | cb T42 |
| T32 | A KEK, or the whole secrets directory, leaked | Everything goes: stop, new KEK, `keys wipe` with the services down (connection rows to `needs_reconnect` without credentials, TOTP reset), older backups treated as compromised; the owner rotates the Withings client secret so stolen refresh tokens are useless; a compromise of the KEK ring is a seed compromise too, so the seed and pepper are rotated and a `compromise` rotation retires every old webhook secret before it adds the new one, and subscriptions without tokens are revoked through `notify list` on reconnect (accepted risk 20); no upstream revoke (stolen access tokens live up to 3 h: accepted risk 19) (ADR 0004 §3, ADR 0008) (amended 2026-10-31, #49 and #72) | #13 [test_keys_wipe.py], [test_keys_rewrap.py]; #22 [test_withings_oauth.py] (`test_rotate_never_downgrades_a_pending_compromise`); #24 [test_withings_webhook.py] (the worker's rotation), [test_withings_schema_functions.py] (`test_a_compromise_append_refuses_an_unretired_entry_or_a_reused_seed`); owner (runbook, M2) | cb T40 |
| T33 | The current KEK lost | KEKs backed up separately from the dumps; restore drill; without the KEK every user reconnects and re-enrols, by design | owner, M2 (backups, restore drill) | new |
| T34 | A stale refresh, reconnect or 401 overwriting newer tokens or flipping a fresh connection | `token_generation` raised by every token write; token writes and status changes are compare-and-swap on the generation read; rewrap doesn't change it (ADR 0004 §6) | #13 [test_connections_generation.py] (`test_a_stale_generation_cannot_overwrite_tokens`); #22 [test_withings_tokens.py] (`test_an_older_generation_cannot_overwrite`) | cb T77 |
| T35 | Losing the only valid refresh token, or using the new access token before the new pair is saved | PLAN §4.3: per-connection lock, durable intent, CAS write, first use after the commit, one recovery retry within the 8-hour grace, then `needs_reconnect`; never the generic retry; a session-scoped advisory lock across the intent commit; reconnect clears a stale intent (ADR 0004 §6, §7) | #22 [test_withings_tokens.py] (`test_a_crash_after_the_response_recovers_once`, `test_a_crash_after_commit_keeps_the_new_generation`, `test_two_workers_perform_one_refresh`, `test_a_stale_intent_needs_reconnect`); #30 (the failure-injection suite) | new |
| T36 | Withings OAuth abuse: forged state, code injection, a victim's session finishing an attacker's flow, a Withings account already linked to another ownvitals account | State hashed, bound to the session, 10 minutes, single use, taken before the exchange, kept in the web process while web runs as a single process (amended 2026-11-03, #16); the code exchanged in the request through web's one narrow egress entry (roadmap Amendment 1); a Withings user linked to another account refused, and a different Withings account refused while one is connected; connect needs recent auth and step-up; `fake-withings` only in tests (ADR 0004 §7) | #13 [test_connections.py] (step-up on connect); #22 [test_withings_oauth.py] (`test_callback_refuses_a_replay_and_a_foreign_state`, `test_an_expired_state_is_refused`, `test_a_withings_user_linked_elsewhere_is_refused`, `test_a_different_live_withings_account_is_refused`, `test_web_cannot_refresh_revoke_or_call_another_withings_action`), [test_egress_code_exchange.py]; #27 [test_withings_disconnect.py] (`test_a_different_withings_account_is_refused_while_connected`). Caveats tracked in #165: the expiry test checks 601 s, not the 600 s boundary, and `test_web_cannot_refresh_revoke_or_call_another_withings_action` is flagged there as passing for the wrong reason. The state is kept hashed in the web process, not in a table, while web runs as a single process (ADR 0004 §7, amended 2026-11-03, #16; accepted risk 22); a table before more web replicas or the M8 VPS move (#165) | new |
| T37 | The Withings app client secret leaking | Enveloped in `withings_apps` with AAD bound to the app; web (the code exchange) and the worker (refresh, revoke) hold it in memory only for the call; the app role reads it through a column grant; no runtime role (`ownvitals_app`, `ownvitals_maint`) can write the table, and maint reads only its non-secret columns; the configuration is written, and the client secret rewrapped after a KEK rotation, only by #22's tool as the owner role, from one-off operator containers, with an audit row in the same transaction; never logged; rotated by the owner at Withings after any suspected leak (ADR 0002 §3, ADR 0004 §2, §3) (amended 2026-10-31, #49) | #21 [test_maint_privileges.py] (guard 11's `withings_apps` check), [test_withings_schema.py]; #22 [test_withings_oauth.py] (`test_the_app_role_cannot_write_withings_apps`, `test_the_operator_command_prints_no_secret`), [test_crypto_guard.py]; owner (runbook) | new |

### D. The Withings webhook (PLAN §4.4; ADR 0002 §4, ADR 0004 §4, ADR 0005 §8, ADR 0008; amended 2026-10-31, #72)

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T38 | Forged notifications from anyone who knows the URL pattern, a webhook secret recovered from a dump, or an entry planted or garbled in `webhook_secrets` | A 256-bit secret per app in the path, derived from an off-database seed that only the worker holds, never stored; the database keeps only a peppered HMAC with the secret, seed and pepper ids, which web (pepper only) compares in constant time with the pepper the entry names; a retired entry stops verifying at once; no oracle: the request checks run before the secret lookup, and an unknown or retired secret gets the generic 200 from web's in-process copy of the entries, before any database work, and is ignored and counted in a metric (O14); while the copy is older than the entry-cache TTL, or before web has read one at all, every `POST` gets 503, whatever the secret, and Withings retries (`HEAD` stays 200); rotation in the order `rotation_reason` names (ADR 0008): a `compromise` rotation retires every old entry in one atomic call before it appends the new one, and the function refuses an append that would clear a `compromise` request while another entry is not retired, or under a seed a retired entry uses (a guard against an honest worker's mistakes, not against code with the app role: accepted risk 21); the function compare-and-clears the request under its row lock (a routine step against a pending `compromise` is refused as `compromise_pending`, a step whose request changed after the step read it as `stale_request`), and `rotate` never downgrades a pending `compromise` (one conditional `UPDATE`), so a compromise request is never lost; each rotation step runs at READ COMMITTED under an advisory lock per app (namespace 4) and decides from what it read under it, so several workers act on a request one step at a time; a refused step raises an alert, so, with the worker up, a compromised secret doesn't stay verifying, except that code running with the app role could stall a compromise rotation by holding lock 4 or the `withings_apps` row lock: the role's idle-in-transaction timeout and, from #77, its `transaction_timeout` (ADR 0002 §5) end a holder that runs at the role's settings, but such code can lift both for its own session or hold lock 4 at session level, so for it the stall is denial of service, surfaced by the stuck-rotation alert (#24) and accepted under risk 21 (amended 2026-10-31, #78); entries written only through the webhook-secret definer function, owned by the narrow `NOLOGIN` role `ownvitals_webhook_secrets` (no DDL, no other column, no health table), with input rules, an audit row that never holds an `hmac` or anything derived from a secret, and called only by the worker's rotation module (guard), though code running in web could call it too (accepted risk 21); the payload is only a hint: data is always fetched from the API with the user's own token (ADR 0002 §3, ADR 0004 §4, ADR 0008) (amended 2026-10-31, #49 and #72) | #24 [test_withings_webhook.py] (`test_bad_notifications_create_no_job_and_do_not_log_the_secret`: forged, oversized, malformed, replayed; `test_both_secrets_work_during_overlap_and_the_retired_one_is_ignored`; `test_a_cold_cache_refuses_every_post_and_head_stays_200`; `test_a_held_lock_and_cancelled_repoint_jobs_raise_the_stall_alert`; `test_a_retired_seed_rebuilds_an_old_url_and_cannot_mint`), [test_withings_webhook_callers.py] (the single-caller guard); #21 [test_withings_schema_functions.py] (`test_append_retire_and_the_audit_row_hold_no_hmac`, `test_a_compromise_append_refuses_an_unretired_entry_or_a_reused_seed`, `test_compare_and_clear_refuses_a_request_that_moved`, `test_the_webhook_secret_role_has_no_bypass_and_no_client_secret`); #22 [test_withings_oauth.py] (`test_rotate_never_downgrades_a_pending_compromise`, concurrent); #8 [test_keks.py] (`test_web_holds_the_kek_and_the_pepper_and_never_the_seed`, `test_web_refuses_to_start_when_it_can_see_a_seed_file`); #77 [test_role_check.py] (`transaction_timeout`). Open hardening in #165: a database error after a secret match answers 500, not the generic 200 | new |
| T39 | Replayed or flooded notifications with a valid (leaked) secret, burning the shared 120/min budget | Dedupe key; singleton jobs with debounce; per-connection and per-app quotas in the ingest function; lanes keep "sync now" first; the worst case is rate budget, never data; rotation with two active secrets | #24 [test_withings_webhook.py] (`test_a_crash_after_commit_leaves_one_event_and_redelivery_collapses`: dedupe); #18 [test_jobs_withings.py] (singletons, debounce, lanes); #23 [test_withings_rate.py] (`test_a_601_storm_pauses_and_then_resumes`). **The per-connection and per-app quotas in the ingest function are not built** (#165, `webhook_event_insert`) | new |
| T40 | Oversized or malformed bodies against the parser | `HEAD` → 200; `POST` only form-encoded, over 2 KB refused before parsing, strict fields with sane date bounds | #24 [test_withings_webhook.py] (`test_bad_notifications_create_no_job_and_do_not_log_the_secret`: a body over 2048 bytes, malformed, wrong type, dates out of range) | new |
| T41 | The secret leaking through logs or traces, or through `notify list` responses, which carry every subscription's callback URL | Path redacted in app logs, traces and errors; callback URLs, built or listed, never stored, logged or used as metric labels; SWAG's webhook location logs without the URI; log-redaction tests. Cloudflare, if proxied, still sees it (accepted risk 5) (ADR 0004 §9, ADR 0008) (amended 2026-10-31, #49 and #72) | #24 [test_withings_webhook.py], [test_log_redaction.py] (the secret part of a webhook path); #22 [test_withings_oauth.py] (`test_connect_urls_never_carry_a_secret`); #26 [test_withings_reconcile.py] (`test_subscription_drift_is_repaired_without_logging_urls`); #28 [test_metrics.py] (no URL in a label); owner, M2 (O6) | new |
| T42 | Probing the webhook to learn which Withings users are linked | A generic 200 for an unknown user; internal logging rate-limited | #15 [test_isolation_contexts_webhook.py] (`test_a_notification_for_an_unlinked_user_makes_no_job`: the generic 200); #24 [test_withings_webhook.py] (an unknown appli, a forged secret: the same answers). **The rate limit on the internal unknown-user log line is untested** (gap G3, #165) | new |
| T43 | Missed, late, duplicated or out-of-order notifications leaving data incomplete while the UI implies "nothing" or "normal" | Sync cursors, not webhooks, define correctness; poll every 6 h and reconcile nightly; deletions only after two complete reconciliations; per-stream completeness and capability states; missing data never shown as zero; a subscription that can't be re-pointed within 14 days is resubscribed on the next token use (#26) or reconnect (#22), and subscriptions on old callback URLs are revoked through `notify list` on connect and reconnect, found by reconciliation and revoked at disconnect (D3, PLAN §4.2, §4.4; ADR 0008) (amended 2026-10-31, #49 and #72) | #25 [test_withings_sync.py] (`test_an_unsupported_stream_is_not_stored_as_zero`, `test_an_outage_longer_than_a_day_fetches_the_missed_sample`); #26 [test_withings_reconcile.py] (`test_a_row_is_deleted_only_after_two_complete_passes`, `test_subscription_drift_is_repaired_without_logging_urls`, `test_a_marked_connection_is_resubscribed_after_cleanup`); #22 [test_withings_oauth.py] (`notify list` clean-up in `test_connect_urls_never_carry_a_secret`); #27 [test_withings_disconnect.py]; #164 [test_worker_kinds.py] (the poll and reconcile schedule); #30 (PLAN §4.4 checklist). Open in #165: nothing writes the `resubscribe` mark yet | new |

### E. Jobs, worker and egress ([ADR 0005](../adr/0005-jobs-and-leases.md))

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T44 | The queue's definer functions used to read or move other users' jobs | Three functions owned by `NOLOGIN` `ownvitals_queue`, fixed `search_path`, no caller ids, minimal returns; installed by the superuser script; md5-pinned startup check; no other user code in the database (ADR 0005 §3) | #9 [test_queue_functions.py], [test_queue_guard.py] (guard 9), [test_db_functions_script.py], [test_install_functions_sql.py]; #101 [test_compose_host_sql.py] | cb T45 |
| T45 | A worker that lost its lease writing job state or health rows | The fencing token checked first in every worker transaction and held to the commit; health rows, revisions and cursors written in fenced transactions (ADR 0005 §4) | #9 [test_jobs_fence.py], [test_jobs_worker.py]; #25 [test_withings_sync.py] (`test_backfill_resumes_after_a_lost_lease_and_a_rerun_ingests_nothing`) | cb T12 |
| T46 | A repeated request creating duplicate jobs | `Idempotency-Key` with a request hash, 409 on another body (ADR 0005 §2) | #9 [test_jobs_api.py] (`test_idempotency_same_key_same_body_other_body_and_the_limit`) | cb T46 |
| T47 | Job events or SSE leaking another user's progress, secrets or health values | Owned events read per request; 404 for foreign ids; payload models allow enums, own ids, counts and dates only (ADR 0005 §7) | #9 [test_jobs_events.py], [test_jobs_stream.py] (`test_a_stream_never_serves_another_users_job`, `test_rls_alone_hides_another_owners_job_and_events`), [test_auth_session_probe.py] | cb T48 |
| T48 | Web or worker reaching arbitrary hosts (SSRF, exfiltration) | One module builds every client; per-process allowlists (worker: Withings hosts, Anthropic only with AI on; web: Google sign-in, the SMTP server for account mails and for the outbox mail that a background task in web sends, and, from M1, only the Withings authorization-code exchange, [roadmap Amendment 1](../roadmap.md)); SMTP from web and the operator CLI through one mail module, never from the worker, whose mail goes into the outbox of ADR 0007, whose rows name a fixed kind and a recipient, never an address or free text, so no row can send mail to an outside address (ADR 0003 §6; amended 2026-10-31, #49 and #72); https, port 443, every redirect hop checked; total deadlines and request budgets; no TLS key log; no URL from user or provider input is fetched | #6 [test_egress.py], [test_egress_guard.py], [test_egress_deadline.py], [test_egress_budget.py], [test_egress_runtime.py] (`test_what_each_process_can_reach`, `test_mail_is_refused_in_the_worker_before_any_connection`), [test_egress_hook.py] (ADR 0009), [test_mail.py] (`test_the_worker_never_sends_mail_even_to_the_configured_host`, `test_the_smtp_tls_context_ignores_sslkeylogfile`); #22 [test_withings_oauth.py] (`test_web_cannot_refresh_revoke_or_call_another_withings_action`), [test_egress_code_exchange.py]; #194 (outbox rows hold no address or free text; web's list unchanged) | cb T19, T49 |
| T49 | A poison job or retry storm exhausting the shared Withings budget for everyone | Retry budget, capped backoff, non-retryable categories, poison rows failed by the reaper; M1: tokens consumed when claimed and never refunded, 601 pauses the bucket, refresh never retried generically (ADR 0005 §6, §8) | #9 [test_jobs_worker.py], [test_jobs_errors.py], [test_jobs_runner.py]; #18 [test_jobs_withings.py] (retry budget, dead-letter); #23 [test_withings_rate.py] (`test_a_601_storm_pauses_and_then_resumes`, `test_a_crashed_call_keeps_its_token_and_frees_its_slot_at_expiry`, `test_the_app_role_cannot_refund_a_token`) | cb T50 |
| T50 | A job continuing for a disabled or deleted user | Owner checked after every claim; jobs cascade with the user, so the fence fails on the next write (ADR 0005 §4) | #9 [test_jobs_fence.py] (`test_an_inactive_owner_fails_the_job_with_account_disabled`), [test_jobs_schema.py] (cascades) | cb T53 |
| T51 | One user's backfill starving another's sync | Lanes, per-owner fairness, a capped backfill share (ADR 0005 §8) | #23 [test_withings_rate.py] (`test_one_accounts_backfill_cannot_starve_anothers_sync`, `test_the_same_lane_is_round_robin`); #18 [test_jobs_withings.py] (lanes in the queue) | new |

### F. Admin, flags and the browser

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T52 | Admin account compromise | TOTP for every account; the admin role only with TOTP within 12 h; step-up within 5 min for admin writes; role changes from the CLI only; every admin route guarded | #12 [test_admin_routes_guard.py], [test_admin_stepup_window.py]; #11 [test_auth_mfa.py] (the admin gate); #10 [test_auth_cli.py] (role changes from the CLI only) | cb T15, T90 |
| T53 | Admin misuse, or the audit log altered to hide it | Every admin write audited in its own transaction, refusals too; rows hold keys and meanings, never secrets, tokens or health values; append-only for maint | #12 [test_admin_db.py] (the audit row first, refusals too), [test_admin_audit_routes.py], [test_admin_settings.py], [test_admin_users.py], [test_maint_privileges.py] (neither runtime role can change the audit log). Open in #165: `enable`/`delete` audit rows written in a later transaction | cb T16, T92 |
| T54 | A flag loosening a protection: the experimental tier beyond the household, AI on without consent, open sign-up | Stored flags joined with the environment, stricter wins; `experimental_findings` owner-only, default off (D9); `ai_insights` default off (D11); unknown keys refused; invalid values fall back to the stricter default | #12 [test_flags_db.py], [test_admin_settings.py]; #10 [test_flags.py] | cb T94 |
| T55 | Script injection through user, admin or provider text (a banner, device names, Withings fields) | React escaping only, no raw HTML; CSP `'self'` only, no inline script or style, `frame-ancestors 'none'`, `Referrer-Policy: no-referrer`; no third-party host at runtime | #2 [test_web.py] (CSP, `frame-ancestors`, `Referrer-Policy`); #3 [sources.test.ts] (no remote URL, no CSP violation in shipped sources) | cb T20, T95 |
| T56 | Cross-site requests, or health data cached by a browser or proxy | Origin check and CSRF token on every write; `Cache-Control: no-store` on every API response | #10 [test_auth_csrf.py] (Origin, CSRF token and `Cache-Control: no-store` on every API path); #12 [test_admin_routes_guard.py], [test_admin_settings.py]; #9 [test_jobs_stream.py] | cb T93 |

### G. Logs, metrics and mail

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T57 | Health values, tokens or secrets in logs, errors or traces | Secrets only from files, as `SecretStr`; JSON logs with a redaction filter; no exception text in responses; logs never hold measurement values, ECG samples, tokens, webhook secrets or full webhook paths (AGENTS.md) | #2 [test_logging.py], [test_log_redaction.py], [test_log_fail_closed.py], [test_log_chain.py]; #10 [test_auth_logging.py]; #13 [test_connections.py]; #20 [test_withings_client_logs.py]; #24 [test_withings_webhook.py] | cb T7 |
| T58 | Health values or user identifiers exported as Prometheus metrics | Labels limited to app, stream and category; no user id, Withings user id or value in a label | #28 [test_metrics.py] (`test_label_names_that_could_hold_identifiers_are_refused`, `test_rendered_output_has_no_identifier_text`); #167 wires the recorders | new |
| T59 | Health data in emails, which pass through a relay and mailboxes outside our control | Notification mails say something is waiting and link to the app; no values, finding text or ECG results; the worker never opens SMTP: its mail goes into the outbox of ADR 0007, whose rows hold only a fixed kind, the recipient (an owned row's `owner_id`, or a system row for an owner notice) and at most a link chosen from fixed routes, never an address, subject, free text, value or finding text; web resolves the address through `ownvitals.auth` when it sends and builds the mail from the kind's fixed template, through the guard-listed mail module; in M1 the owner notice is in-app only (ADR 0003 §6, ADR 0007; amended 2026-10-31, #49 and #72) | #6 [test_mail.py], [test_egress_runtime.py] (the worker never opens SMTP); #194 builds the outbox and its template test; #195 the finding-notification kinds (O8) | new |

### H. Hosting, supply chain and agents

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T60 | Compromise of the shared home host (torrent client, flaresolverr, Portainer with the Docker socket) exposing the database, KEKs and secrets | Host firewall (8096 only from SWAG), own Docker network, Postgres unpublished, KEK files read-only outside the database, a deploy key without shell; accepted for the household only (accepted risk 1); EU VPS before M8 (§9.2, D8) | owner, M2; M8 | new |
| T61 | Backup theft or loss | Nightly encrypted `pg_dump`, KEKs backed up separately to another machine; RPO 24 h, RTO 4 h; quarterly restore drill; the real-data gate needs a passing drill (§9.2) | owner, M2 | new |
| T62 | A malicious or tampered image deployed, including one built by a `v*` tag pushed on a commit outside `main` (the `release tags` ruleset stops a tag being moved or deleted, not created, and such a tag runs that commit's own `release.yml`) | `release.yml` attests build provenance; **the deploy is the boundary**: the forced-command key takes a tag and a digest and, in order: (1) validates the tag as strict SemVer (`vMAJOR.MINOR.PATCH`, an optional pre-release, no build metadata) and the digest as `sha256:` plus 64 hex, before using either; (2) resolves the tag's commit through `commits/refs/tags/<tag>`; (3) checks that `refs/heads/main` contains that commit (the compare from it to `refs/heads/main` is `identical` or `ahead`); (4) runs `gh attestation verify` on the digest with `--cert-identity https://github.com/magpern/ownvitals/.github/workflows/release.yml@refs/tags/<tag>`, `--source-ref refs/tags/<tag>`, `--source-digest <commit>` and `--deny-self-hosted-runners`; then it pulls by digest; it has no shell. A tag `creation` rule would add nothing while the owner's token is the only pusher (Architect decision). Deploys only on the owner's go-ahead (§9.2) (amended 2026-10-31, #49) | #7 `release.yml` attestation, `scripts/test-release.sh`; #84/#101 [test_deploy_refusals.py], [test_deploy_script.py], [test_deploy_mutations.py], [test_deploy_versions.py], [test_deploy_failures.py], [test_deploy_db_functions.py]; #57 aligned [docs/dev/release.md](../dev/release.md); installing the script is the owner's (M2) | new |
| T63 | Exposed services or container breakout | Non-root image; local ports on 127.0.0.1; Postgres never published; only SWAG reaches web | #2 compose: the repository's `compose.yaml` is the local stack (ports on 127.0.0.1, Postgres included); the production `compose.yaml` on the home server is the owner's ([docs/dev/deploy.md](../dev/deploy.md)) and **no test checks it publishes no Postgres port** (gap G6, O6); [test_image.py], [test_compose_roles.py]; #14 `e2e/scripts/stack-checks.sh` (non-root, no secret in `docker inspect`), CI step "Stack checks" | cb T21 |
| T64 | Cloudflare and SWAG seeing plaintext (pages, API responses, webhook URLs); plain HTTP from SWAG to web on the LAN | SSL Full (strict); firewall; accepted for the household (accepted risk 4); Cloudflare is a processor question at M8 (§9.3) | owner, M2 (O12); M8 | new |
| T65 | A compromised dependency, base image or Action | Lockfiles, exact versions, SHA-pinned Actions, checksum-verified tools, Dependabot, least-privilege `permissions`, `persist-credentials: false`; Trivy image scan | #1 CI (SHA-pinned Actions, checksum-verified tools: `scripts/ci-install-release.sh`; pip-audit, npm audit); #7 Dependabot; [test_image.py] (`test_every_image_the_build_pulls_is_pinned_by_digest`); #14 Trivy image scan (CI step "Trivy image scan") | cb T17 |
| T66 | An agent committing a secret or weakening a check | gitleaks on each PR's commits and weekly on the history; frozen-spec guard; the review gate; the ruleset's required checks | #1 CI `checks` job: "Secret scan" (gitleaks), "Frozen specification unchanged"; the review gate; the ruleset's required checks | cb T18 |
| T67 | Real health data reaching agents, fixtures, tests, logs, PRs or issues | No real data on the development machine; synthetic, labelled fixtures; `fake-withings` and the demo account only; the probe prints shapes only and the owner runs it; agents never connect a real account (AGENTS.md, §10.4) | #1 AGENTS.md and review checks; #29 [test_withings_probe.py], [test_withings_probe_db.py] (shapes only); #19 [test_withings_fake_contract.py] (`test_fixture_is_labelled_synthetic_and_matches_the_documented_shape`, `test_every_response_is_labelled_synthetic`), [test_withings_fake_generator.py]; the whole suite's network guard [test_netguard.py] | new |
| T68 | Prompt injection of the development agents through issues, PRs or upstream text | Repository and web content treated as data; the review gate and ruleset on every merge; no secret in any prompt | #1 AGENTS.md, `.claude/agents/` | cb T31 |
| T69 | The public repository telling an attacker the design | No control relies on secrecy of the design; secrets never in the repository | #1 gitleaks; accepted | new |

### I. Health-data lifecycle and use (M1 to M7)

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T70 | Health data surviving account deletion, or a disconnect that silently keeps upstream access | `owner_id` references `users (id) ON DELETE CASCADE` on every owned table (ADR 0002 §2); system tables with user-linked rows purged (`webhook_events` after 30 days and on deletion, `auth_events`); upstream revoke before the purge (PLAN §4.3); disconnect revokes every subscription `notify list` returns (old URLs included), deletes the secrets even when a revoke fails and records the failure, and keeps imported data unless the user asks to delete it too (ADR 0004 §7); backups age out (accepted risk 13) (amended 2026-10-31, #49) | #27 [test_withings_disconnect_account.py] (`test_account_delete_revokes_and_removes_every_owned_row`), [test_withings_disconnect.py] (`test_a_failed_revoke_still_removes_secrets`); #163 [test_withings_disconnect_routes.py] (revoke before the purge, `webhook_events` purged); #10 [test_auth_purge.py]; #218 extends "delete imported data" to derived rows | cb T22 |
| T71 | Another household member's measurement attributed to the user (`attrib=1`) | Kept as Withings reports it, excluded from analysis and shown as "unconfirmed" (PLAN §7.1) | M4 (PLAN §7.1); accepted risk 14 | new |
| T72 | Misleading health information: a sparse series shown as a trend, estimates shown as certified, experimental interpretations shown to the wrong people | Minimum-data policy, confidence and data quality on every finding; tiers, the experimental tier behind the owner-only flag; estimates never shown as Withings' values (D9, PLAN §7) | M4, M5 (PLAN §7); M8 medical-claims review | new |
| T73 | Health data sent to a model provider beyond need or without consent | Opt-in flag, explicit consent, minimal tool results, provider review, audit with redacted content, prompt-injection evals; experimental findings never for non-household users; never tokens (PLAN §7.4) | M7: section K, T96 to T105 (ADR 0014; #205 to #208) | cb T32 |
| T74 | A stolen session exporting a user's full history | Export requires recent authentication and step-up, download a fresh step-up (PLAN §6, ADR 0013 §4) | M7: section K, T90 (ADR 0013; #211) | new |

### K. Reports, export and AI insights (M7; [ADR 0013](../adr/0013-reports-and-export.md), [ADR 0014](../adr/0014-ai-insights.md), proposed 2026-11-03, #204)

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T90 | A stolen live session requesting and downloading the whole history | Request behind recent authentication and step-up; download behind a fresh step-up; one export in progress, 3 per 24 h; every request and download audited and listed in the security activity (ADR 0013 §4, §5) | M7 export issue (#211): `test_auth_stepup.py` extended, `test_exports_api.py`, `test_exports_download.py` | new |
| T91 | The export reachable through a bearer URL in logs, a `Referer`, history or a mail | No URL secret: the path holds only the export id, the session cookie and step-up authorise; `Cache-Control: no-store`; a download without `Sec-Fetch-Site: same-origin` refused; the ready mail carries no link to the file (ADR 0013 §4) | M7 export issue (#211): `test_exports_download.py`, the logging test | new |
| T92 | One user's export containing another user's rows | The build job runs in the requester's `UserContext`: owned tables under forced RLS; account data (`users`, `auth_events`, system tables not under RLS) only through `ownvitals.auth`'s `export_account(ctx)`, keyed by the context's user (guard 8); chunks are owned rows; 404 for another user's id; the only cross-user path is the purge's definer function (ADR 0013 §4) | M7 export issue (#211): `test_export_isolation.py` (two users, owned and account tables); RLS tests in the worker context; `test_exports_purge.py` | new |
| T93 | Secrets, hashes or ciphertext in the export; a new column exported by accident | A table-and-column allowlist with a guard over every owned table and every account system table; every secret kind planted and grepped in the archive (ADR 0013 §1) | M7 export issue (#211): `test_export_contents.py` | new |
| T94 | Formula injection through exported CSV opened in a spreadsheet | Text cells starting with `=`, `+`, `-`, `@`, tab or CR prefixed with `'` (ADR 0013 §1) | M7 export issue (#211): `test_export_csv.py` | new |
| T95 | Estimates, experimental or hidden findings, scores or unconfirmed readings in the doctor PDF or in an unflagged account's export, read as Withings' or as clinical | The PDF excludes them all and states attribution and "not a medical device"; the export withholds estimates and experimental findings from unflagged accounts and labels estimates in their own file (ADR 0013 §2, §6; ADR 0006 §9, §11) | M7 export and PDF issues (#211, #210): `test_export_tiers.py`, `test_doctor_pdf.py` | new |
| T96 | AI calls made without the feature, the provider review, the user's consent or budget | Five locks checked by the worker before every call (feature, provider review, eval passed for this model and prompt, consent, budget); defaults keep AI off (no review or eval reference, no model, caps 0) (ADR 0014 §1) | M7 AI issues (#205 to #208): `test_ai_gates.py`, `test_ai_consent.py` | cb T32 |
| T97 | More health data sent to the provider than the question needs, or identifiers, raw signals or free text | Read-only tools with closed arguments and an output allowlist; no ids, emails, places, raw payloads, signals or times of day; Withings free text mapped to closed sets; result size and tool-call caps (ADR 0014 §4) | M7 AI issues (#205 to #208): `test_ai_tools_schema.py` | cb T32 |
| T98 | Experimental findings or estimates reaching the model for an account outside the experimental gate (a non-household user from M8) | Tier filter in the tool layer, not the prompt; experimental scores (the HIS) included; `ai_include_experimental` defaults to false; hidden findings and stale estimates absent for everyone (ADR 0014 §4) | M7 AI issues (#205 to #208): `test_ai_tools_tiers.py`; the tier eval | new |
| T99 | Prompt injection through the user's text or Withings data leading to disclosure, actions or another user's data | Tools in the owner's `UserContext` with no user argument; no side-effect tools; no secret in any request; the eval suite's injection categories with thresholds (ADR 0014 §5, §8, §9) | M7 AI issues (#205 to #208; the digest, #209, joins #208's evals): `test_ai_tools_isolation.py`, `test_ai_request.py`; the CI and owner-run evals | new |
| T100 | Model output carrying script, a phishing link or diagnostic text shown as the app's own | Plain-text rendering, links not clickable, the CSP; the "AI-generated, can be wrong, not medical advice" label; the health-claims eval at 100 % on its must-not set (ADR 0014 §5, §9) | M7 AI issues (#205 to #208): `test_ai_render.test.tsx`; the health-claims eval; M8 medical-device review | new |
| T101 | The cost cap overshot by concurrent calls, a crash between call and settle, or a price change | Reserve an upper bound before every call (global then user row locks) as a reservation row; settle or release through definer functions (installed by the superuser `db-functions` script, digest-pinned; the app role can't write the spend tables) that move a reservation's state once (`WHERE state = 'reserved'`) in the same transaction as both counters, so a second settle is a no-op; timeouts, 5xx and crashes charged at the reserved amount; the reaper's `ai_settle_lost()`; `CHECK`s against negative counters; unpriced models refused (ADR 0014 §7) | M7 AI issues (#205 to #208): `test_ai_cost_cap.py` (PostgreSQL: 50 concurrent reservations, double settle, settle then crash), `test_ai_pricing.py` | new |
| T102 | Processing or stored conversations continuing after consent is withdrawn | Withdrawal in one transaction: consent closed, AI jobs cancelled, conversations, messages and digests deleted; the reply's insert and the withdrawal serialise on the consent row, so a reply is either deleted by the withdrawal or never stored; the worker re-checks consent before every call (ADR 0014 §3) | M7 AI issues (#205 to #208): `test_ai_consent.py` | new |
| T103 | Conversation content in the audit log or application logs | The audit row holds tool names, enum arguments, token counts, cost and hashes, never text or tool results; request and response fingerprints are keyed HMACs, not plain hashes; the redaction test (ADR 0014 §6) | M7 AI issues (#205 to #208): the logging test extended | new |
| T104 | The model API key leaking, or reachable from web | Secret file mounted into the worker only; the model client built only in a worker with AI on (ADR 0009); never logged or stored (ADR 0014 §1) | M7 AI issues (#205 to #208): the compose checks, `test_egress_runtime.py` | new |
| T105 | The owner-run eval calling the model with real data, or used to bypass the locks | An eval process role of its own: hook kind `eval` and an allowlist of `api.anthropic.com` only (no Withings, Google or SMTP host), admitted by the guard's `HOOK_INSTALLERS` and `EVAL_ROLE_MODULES`; refusing the app's database and any database not named `*_ai_eval`, and stopping if any account there lacks the seeder's `synthetic` marker; its own owner-set lock, the provider review and model still required, its own cap through the reservation functions; audit rows of kind `eval` (ADR 0014 §9) | M7 AI issues (#205 to #208): `test_ai_eval_cli.py` (each refusal makes no model request), `test_egress_runtime.py`, `test_egress_hook.py` (`eval` child), `test_egress_guard.py` | new |

### J. Passkeys and QR approve-login (M3; [ADR 0011](../adr/0011-passkeys-and-qr-login.md), proposed 2026-11-03, #177)

| # | Threat | Mitigation | Enforced by | From |
|---|---|---|---|---|
| T76 | A passkey assertion phished or replayed through another origin, or a stale or foreign challenge | One accepted origin (the public base URL), `rp.id` hash, type and challenge checked; challenges 32 random bytes, hashed, single-use (`DELETE … RETURNING`), 5 minutes, bound to the session or pre-session; UP and UV flags required (ADR 0011 §1) | M3 passkey issue (#178): `test_passkey_register.py`, `test_passkey_signin.py` | new |
| T77 | A passkey alone reaching health data, so a stolen or synced passkey bypasses mandatory TOTP | A passkey replaces only the first factor; the session is full only after a TOTP or recovery code; a passkey sets recent auth, never step-up; `passkey_satisfies_second_factor` refuses true until the owner decides (ADR 0011 §3) | M3 passkey issue (#178): `test_auth_mandatory_totp.py` extended, `test_passkey_reauth.py`, `test_config.py` | new |
| T78 | A stolen live session adding the attacker's passkey, or removing the owner's | Add and remove need recent authentication and step-up; each audited and listed in the security activity (ADR 0011 §8) | M3 passkey issue (#178): `test_auth_stepup.py` extended; `passkey.spec.ts` (#180) | new |
| T79 | QR login-jacking: a phishing page shows the victim a QR from the attacker's desktop, the victim scans and approves, the attacker's desktop gets the victim's session | No push (approval starts only from a scan); a TOTP or recovery code entered for each approval, bound to its row (a step-up window does not count); the screen shows browser, OS, IP and place "as reported" and says to approve only a computer in front of you; a different-network warning; 3 approvals per hour; every completed approval mailed and listed; a QR-born session can't approve, carries no step-up, recent auth or admin role (ADR 0011 §5, §7). Residual: relay phishing: a page that relays the attacker's QR and gets the victim to scan it and type a fresh code still hands over a session, since a TOTP is not phishing-resistant; the code adds friction and a moment of attention, not a cryptographic binding to the desktop; owner question 3 re-decides the feature at M8. As built (#179): the browser, OS and place are the desktop's own claims, and with `geo_source=none` (the default) the place is always "Place unknown", so the IP and the different-network warning are the only location signals the approver gets | M3 QR issue (#179): `test_auth_qrlogin.py`, `test_auth_qrlogin_factor.py` (the code per approval, the 3-per-hour limit), `test_auth_qrlogin.py` (the network flag, the mail, no chaining); `qr-login.spec.ts` (#180) | new |
| T80 | The QR nonce leaking through logs, a `Referer`, browser history or a screenshot | Nonce only in the URL fragment, removed from history by the SPA, sent in a POST body (never logged); stored as SHA-256; 60 s to scan, single-use; a screenshot is useless after the first scan or 60 s; the SSE path holds only the row id (ADR 0011 §5, §9) | M3 QR issue (#179): `test_auth_qrlogin.py`, `test_auth_logging.py` extended | new |
| T81 | Races: two scans, two completes, approve against deny or expiry, a revoked approver session | Every transition a conditional `UPDATE … WHERE state = … AND now() < deadline RETURNING`; first scan wins; one session per row; complete re-checks the approver's session and account (ADR 0011 §5). Residual (#179 review): "log out everywhere" and a password change revoke sessions without the account-row lock, so one that commits while a QR `complete` holds the row can miss the new desktop session, as it can miss any sign-in racing it; `admin mfa-reset` takes the row lock and leaves nothing | M3 QR issue (#179): `test_auth_qrlogin_races.py` | new |
| T82 | Someone other than the desktop that showed the QR collecting the session (a learned row id, another browser) | Events and complete need the pre-session cookie (`__Host-`, `HttpOnly`, `SameSite=Strict`, 5 minutes) that created the row; the session cookie is set only on that POST, never in an event or body (ADR 0011 §4, §5) | M3 QR issue (#179): `test_auth_presession.py`, `test_auth_qrlogin_sse.py` | new |
| T83 | Session swap: someone else scans the victim's desktop QR first and approves with their own account, so the victim works in, or connects data to, the wrong account | The first scan wins and the victim's later scan is refused, which shows the victim something is wrong; the desktop shows the signed-in account's email after completion; connecting Withings needs that account's recent auth and step-up, which the victim can't give (ADR 0011 §5, ADR 0003 §4). Whoever scans first with a full session also learns the desktop's IP and place: accepted, it is what an approver needs | M3 QR issue (#179): `test_auth_qrlogin.py`; `qr-login.spec.ts` (#180) checks the email shown | new |
| T84 | Spoofed place or device details: forged `CF-IPCity`/`CF-IPCountry` or `User-Agent`, or text that misleads in the approval screen | Location headers read only from a listed Cloudflare-facing proxy, once, sanitised and capped, rendered as text; `none` by default; browser and OS labelled "as reported"; place never used in a security decision (ADR 0011 §6, §7); SWAG strips the headers from non-Cloudflare peers (O17) | M3 QR issue (#179): `test_auth_qrlogin_geo.py`; owner, O17 | new |
| T85 | A cloned authenticator (sign-count regression) | A non-zero count that does not increase is refused, the credential suspended, the user mailed; the count updated by compare-and-set (ADR 0011 §2) | M3 passkey issue (#178): `test_passkey_signcount.py` | new |
| T86 | Account enumeration or brute force through passkey sign-in | Discoverable credentials: nothing typed; one generic failure for unknown credential, bad signature, suspended credential or regression; failures counted in the sign-in buckets (ADR 0011 §3) | M3 passkey issue (#178): `test_passkey_signin.py` | new |
| T87 | An authenticator or its sync account learning internal identifiers | `user.id` is a random 32-byte handle, never the user id or email; attestation `none`; the AAGUID never trusted (ADR 0011 §1, §2) | M3 passkey issue (#178): `test_passkey_options.py` | new |
| T88 | A passkey, Google identity or known password planted while an attacker held the account surviving an account recovery and winning the first-enrolment race after `admin mfa-reset` | `admin mfa-reset` (and any recovery that resets the second factor) deletes every passkey, unlinks every Google identity and expires the account's live QR rows, and `admin mfa-reset` clears the password hash (ADR 0011 §3 amendment of 2026-11-03, amending ADR 0003 §2); both lock the account row first; a passkey sign-in to an account without a confirmed TOTP is refused, so first enrolment is reachable only through the mailed reset link; a password reset keeps passkeys but its mail lists them for review (ADR 0011 §3); a QR `complete` locks the account row first too (#179). Residual: a QR login that completes just before a reset commits gets a session, which the reset revokes with the rest as soon as it holds the row; no session survives either order | M3 passkey issue (#178): `test_auth_passkeys_recovery.py`; M3 QR issue (#179): `test_auth_qrlogin.py` (`test_mfa_reset_expires_the_accounts_live_rows`), `test_auth_qrlogin_races.py` (complete against reset, both orders) | new |
| T89 | Anonymous clients exhausting web or the database pool through passkey sign-in starts, QR starts or open SSE streams | 20 starts per 10 minutes per IP for each; one live challenge or QR row per pre-session; one stream per row, 5 per IP, 100 per process; one shared `LISTEN` connection per process, streams hold no pooled connection while waiting; expired state purged hourly (ADR 0011 §1, §4, §5). Residual (#165 item for ADR 0011): the 100-per-process stream cap can be filled from many IPv6 /64s (5 each), which denies QR login, and nothing else, until those streams end (3.5 minutes at most) | M3 passkey and QR issues (#178, #179): `test_passkey_signin.py`, `test_auth_qrlogin_sse.py`, `test_auth_purge.py` | new |

## Accepted risks (household phase)

Each holds only while every user is a household member; the M8 gate re-decides all of them. Items 1,
3, 4, 5, 14 and 17 follow from the specification (§9.2, §4.4, §7.1, §10.2). Items 19 to 22 follow
from Architect decisions (2026-10-31, 22 on 2026-11-03; #49 extended 20 and 21; #72 scoped 21's input rules and added
three ways to stall a rotation to it; #78 said which holders the role's timeouts end and added owner
notices to it; #88 added that a rehash can set a value the caller chose, that owner notices can also
build a backlog and that the owner-notice write takes only a kind); the others are proposed by this
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
21. **Code running in web could plant, retire or garble webhook-secret entries, stall a
    rotation or queue owner notices** (amended 2026-10-31, #49, #72, #78 and #88). Web shares
    `ownvitals_app` with the worker, so code execution in web could call the webhook-secret
    function, within its input rules, which bound shape, not intent. The known-id rule and the
    compromise-order rules (`unretired_entries`, `seed_reused`) check only the ids the caller
    names, and the app role can insert `connection_keks` rows itself, so they guard against an
    honest worker's mistakes, not against this code (ADR 0004 §4; amended 2026-10-31, #72). It
    could call the function:
    - to add an entry whose HMAC it chose, under a seed id it inserted itself if it likes, or to
      re-hash an active entry to one (a rehash changes only `pepper_id` and `hmac`; web holds the
      pepper and the function never sees a secret, so the rehash clause of ADR 0004 §4 binds only an
      honest caller; amended 2026-10-31, #88), giving it a webhook URL it can call. Code in web sees
      the paths it serves, so that can be a URL a compromise rotation retired, which then verifies
      again (#72). Payloads are only a trigger to fetch with the user's own token (PLAN §4.4), so
      the worst case is extra syncs within the rate budget;
    - to retire entries, delete retired ones or re-hash entries to any value, including one it chose
      (denial of service, extended by #49 and #88): webhooks are then ignored, and the worker's
      derivation check refuses to build callback URLs from a garbled entry, until a rotation
      replaces it. Scheduled sync and drift reconciliation (#26) cover the gap, and an alert fires
      on the ignored-webhook rate.

    It could also stall a rotation (amended 2026-10-31, #72):
    - by holding the rotation lock (namespace 4, ADR 0008 §2): the worker's steps then fail on
      `lock_timeout` and alert. Taken at session level (`pg_advisory_lock`) on a connection
      that then sits outside any transaction, it is ended by no timeout, only when that
      connection closes (amended 2026-10-31, #78);
    - by calling the function and keeping its transaction open, which holds the `withings_apps`
      row lock and blocks `rotate` too. The role's idle-in-transaction timeout ends an idle
      transaction and, from #77, its `transaction_timeout` a busy one (ADR 0002 §5), but both
      are session defaults that this code can lift for its own session (`SET`), and it can
      open another transaction (amended 2026-10-31, #78);
    - by calling the queue's definer function that cancels a routine rotation's queued
      `notify update` jobs (ADR 0008; #18 builds it), which stalls a routine rotation.

    The timeouts end a holder that runs at the role's settings (a bug, a stuck step), not this
    code (amended 2026-10-31, #78). Each is denial of service, never a secret disclosure, and
    surfaces through the stuck-rotation alert (#24). While a `compromise` request is stalled,
    the old secret keeps verifying; that gives this code nothing more, since code running in
    web already decides how webhooks are answered.

    The function's own role holds no DDL right, no other column and no health table (ADR 0002
    §3), so a bug in it reaches nothing more; the guard keeps every module but the worker's
    rotation from calling it (T38, ADR 0004 §4).

    It could also queue owner notices, once the notifications issue (#194, M6) builds the outbox
    (amended 2026-10-31, #78). Any code with the app role can call the owner-notice write, not only
    the worker's, but it takes only an owner-notice `kind` from a fixed list, its route comes from
    the kind through a fixed mapping inside the function, so a caller can't choose one (amended
    2026-10-31, #88), and the notice goes only to the admins with its kind's fixed template. The
    worst case is unwanted mail to the admins and, in volume, a backlog that delays other outbox
    mail, since the write has no dedupe and no rate limit: denial of service, never a disclosure and
    never mail to anyone else (T48, T59, ADR 0007 §3). The notifications issue (#194) keeps at
    most one pending row per owner-notice kind, deduping on `kind` alone (an owner notice's route
    is fixed per kind). That bounds the backlog, not the send rate (amended 2026-10-31, #88).
22. **The Withings OAuth state lives in the web process** (Architect decision 2026-11-03, #16;
    ADR 0004 §7). It is hashed, bound to the session id, valid 10 minutes and single use, but held
    in memory, not in a shared table. A web restart mid-flow loses it, and the user starts the
    connect again; no state crosses processes, so this holds only while web runs as a single
    process. More than one web replica, or the M8 VPS move, needs the table first (#165) (T36).

## Open items

| # | Item | Who, when |
|---|---|---|
| O6 | SWAG: `CF-Connecting-IP` only from Cloudflare's ranges, the webhook location's `access_log` without the URI, no buffering on the SSE paths (the jobs stream and, from M3, `/api/auth/qr/{id}/events`, ADR 0011 §5); firewall 8096 from SWAG only (T17, T41); the production `compose.yaml` publishes no Postgres port and no port but web's 8096 (T63, gap G6) | owner, M2 |
| O8 | No health data in notification mails: the kinds and their templates (T59; what an outbox row may hold is fixed by ADR 0007; amended 2026-10-31, #72) | #194 (the outbox, its fixed templates and their no-health-data test), #195 (the finding-notification kinds) |
| O9 | A guard that every fixture is labelled synthetic (`synthetic: true` or a `synthetic_` file prefix) (T67). Partly covered: #19's [test_withings_fake_contract.py] (`test_fixture_is_labelled_synthetic_and_matches_the_documented_shape` over `backend/tests/fixtures/withings/`, `test_every_response_is_labelled_synthetic`). Open: a repository-wide guard, and the label on `backend/tests/ecg/golden_estimated.json` (gap G4) | #165 (G4) |
| O12 | Disk encryption on the home server; Cloudflare proxied or DNS-only (T60, T64) | owner, M2 gate |
| O15 | Confirm or reject the accepted risks that don't come from the specification (2, 6 to 13, 15, 16, 18 to 22 (22 added by #16); #49 extended 20 and 21, 21 now covering denial of service; #72 added to 21 the three ways to stall a rotation (the rotation lock, an open transaction holding the `withings_apps` row lock, the cross-user cancel) and said its input rules guard against an honest worker's mistakes only; #78 said that the role's idle-in-transaction and transaction timeouts (from #77) end the open-transaction stall only for a holder at the role's settings, not for code that lifts them for its session; that a lock 4 held at session level, the other stall (the rotation lock), is ended by no timeout; and added owner notices to 21; #88 said that a rehash can set a value the caller chose, that owner notices can build a backlog, that the owner-notice write takes only a kind, and set the two stall routes apart here; amended 2026-10-31, #72, #78 and #88) | owner, before the M2 real-data gate |
| O16 | ADR 0011's owner questions: whether a UV passkey satisfies the second factor or counts as step-up (T77); the source of the city (T84); QR approve-login beyond the household (T79) | owner; defaults in ADR 0011 "Open questions" |
| O17 | SWAG: remove `CF-IPCity`, `CF-IPCountry` and the other `CF-IP*` location headers from requests that did not come from Cloudflare, as for `CF-Connecting-IP` (O6); needed only if `geo_source` becomes `cloudflare` (T84) | owner, before `geo_source=cloudflare` |
| O20 | ADR 0013's owner questions: estimates or experimental items in a doctor-PDF variant (T95); which FIT streams (default off); ECG estimates in an unflagged account's export (T95) | owner; defaults in ADR 0013 "Open questions" |
| O21 | ADR 0014's owner questions: the provider data-handling and retention review (T96, T97), the per-user and global monthly cost caps (T101), the model, the eval run and its reference (T105), experimental content to the model for flagged household accounts (T98), retention of conversations and audit rows | owner, before AI is enabled; defaults in ADR 0014 "Open questions" (AI off) |

Closed by the post-port review (#16, 2026-11-03):

| # | Item | Closed by |
|---|---|---|
| O7 | Metric labels without user ids or values (T58) | #28: [test_metrics.py] (`test_label_names_that_could_hold_identifiers_are_refused`, `test_label_values_outside_the_set_are_refused`, `test_rendered_output_has_no_identifier_text`); #167 wires the recorders through the same registry |
| O13 | After the port: confirm each M0 mitigation against the merged code, replace every planned test name, close or re-home every open item | #16: "Post-port review", the "Enforced by" column and this table |
| O14 | Answer an unknown or retired webhook secret with the generic 200 before any database work, ignore it and count it; run the request checks before the lookup; answer a `POST` with 503 while the entry cache is stale or not yet read, and `HEAD` with 200 (T38; amended 2026-10-31, #49 and #72) | #24: [test_withings_webhook.py] (`test_bad_notifications_create_no_job_and_do_not_log_the_secret`: the same 413, 415 and 400 for a forged and a real secret, the forged one ignored and counted; `test_both_secrets_work_during_overlap_and_the_retired_one_is_ignored`; `test_a_cold_cache_refuses_every_post_and_head_stays_200`). "Before any database work" holds by construction, not by a test: `handle` in `withings/webhooks.py` runs the request checks, then matches the secret in the in-memory entry cache and answers before `_ingest`, its only database call; a test that pins it is on #165. A database error after a secret match still answers 500 (#165) |

O18 and O19 were never assigned.

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

## Test files

Every linked test name above resolves to one merged file under `backend/tests/`, `frontend/src/`
or `e2e/specs/` (checked by #16 against `5efe73c`; the link targets are defined below and render
as the links themselves).

[auth-google.spec.ts]: ../../e2e/specs/auth-google.spec.ts
[auth-password.spec.ts]: ../../e2e/specs/auth-password.spec.ts
[guards.test.tsx]: ../../frontend/src/auth/guards.test.tsx
[sources.test.ts]: ../../frontend/src/sources.test.ts
[step-up.spec.ts]: ../../e2e/specs/step-up.spec.ts
[test_admin_audit_routes.py]: ../../backend/tests/test_admin_audit_routes.py
[test_admin_db.py]: ../../backend/tests/test_admin_db.py
[test_admin_jobs.py]: ../../backend/tests/test_admin_jobs.py
[test_admin_routes_guard.py]: ../../backend/tests/test_admin_routes_guard.py
[test_admin_settings.py]: ../../backend/tests/test_admin_settings.py
[test_admin_stepup_window.py]: ../../backend/tests/test_admin_stepup_window.py
[test_admin_users.py]: ../../backend/tests/test_admin_users.py
[test_auth_cli.py]: ../../backend/tests/test_auth_cli.py
[test_auth_clientip.py]: ../../backend/tests/test_auth_clientip.py
[test_auth_csrf.py]: ../../backend/tests/test_auth_csrf.py
[test_auth_email.py]: ../../backend/tests/test_auth_email.py
[test_auth_logging.py]: ../../backend/tests/test_auth_logging.py
[test_auth_mandatory_totp.py]: ../../backend/tests/test_auth_mandatory_totp.py
[test_auth_mfa.py]: ../../backend/tests/test_auth_mfa.py
[test_auth_mfa_hardening.py]: ../../backend/tests/test_auth_mfa_hardening.py
[test_auth_mfa_verify.py]: ../../backend/tests/test_auth_mfa_verify.py
[test_auth_oauth_handoff.py]: ../../backend/tests/test_auth_oauth_handoff.py
[test_auth_oauth_link.py]: ../../backend/tests/test_auth_oauth_link.py
[test_auth_oauth_pkce.py]: ../../backend/tests/test_auth_oauth_pkce.py
[test_auth_oauth_signin.py]: ../../backend/tests/test_auth_oauth_signin.py
[test_auth_purge.py]: ../../backend/tests/test_auth_purge.py
[test_auth_ratelimit.py]: ../../backend/tests/test_auth_ratelimit.py
[test_auth_reset.py]: ../../backend/tests/test_auth_reset.py
[test_auth_session_probe.py]: ../../backend/tests/test_auth_session_probe.py
[test_auth_sessions.py]: ../../backend/tests/test_auth_sessions.py
[test_auth_sessions_protection.py]: ../../backend/tests/test_auth_sessions_protection.py
[test_auth_signin.py]: ../../backend/tests/test_auth_signin.py
[test_auth_signup.py]: ../../backend/tests/test_auth_signup.py
[test_auth_stepup.py]: ../../backend/tests/test_auth_stepup.py
[test_auth_tables_guard.py]: ../../backend/tests/test_auth_tables_guard.py
[test_compose_host_sql.py]: ../../backend/tests/test_compose_host_sql.py
[test_compose_roles.py]: ../../backend/tests/test_compose_roles.py
[test_config_milestone.py]: ../../backend/tests/test_config_milestone.py
[test_connection_errors.py]: ../../backend/tests/test_connection_errors.py
[test_connections.py]: ../../backend/tests/test_connections.py
[test_connections_generation.py]: ../../backend/tests/test_connections_generation.py
[test_context.py]: ../../backend/tests/test_context.py
[test_crypto_guard.py]: ../../backend/tests/test_crypto_guard.py
[test_db.py]: ../../backend/tests/test_db.py
[test_db_functions_script.py]: ../../backend/tests/test_db_functions_script.py
[test_deploy_db_functions.py]: ../../backend/tests/test_deploy_db_functions.py
[test_deploy_failures.py]: ../../backend/tests/test_deploy_failures.py
[test_deploy_mutations.py]: ../../backend/tests/test_deploy_mutations.py
[test_deploy_refusals.py]: ../../backend/tests/test_deploy_refusals.py
[test_deploy_script.py]: ../../backend/tests/test_deploy_script.py
[test_deploy_versions.py]: ../../backend/tests/test_deploy_versions.py
[test_egress.py]: ../../backend/tests/test_egress.py
[test_egress_budget.py]: ../../backend/tests/test_egress_budget.py
[test_egress_code_exchange.py]: ../../backend/tests/test_egress_code_exchange.py
[test_egress_deadline.py]: ../../backend/tests/test_egress_deadline.py
[test_egress_guard.py]: ../../backend/tests/test_egress_guard.py
[test_egress_hook.py]: ../../backend/tests/test_egress_hook.py
[test_egress_runtime.py]: ../../backend/tests/test_egress_runtime.py
[test_envelope.py]: ../../backend/tests/test_envelope.py
[test_envelope_rules.py]: ../../backend/tests/test_envelope_rules.py
[test_flags.py]: ../../backend/tests/test_flags.py
[test_flags_db.py]: ../../backend/tests/test_flags_db.py
[test_image.py]: ../../backend/tests/test_image.py
[test_ingest_revisions.py]: ../../backend/tests/test_ingest_revisions.py
[test_install_functions_sql.py]: ../../backend/tests/test_install_functions_sql.py
[test_isolation.py]: ../../backend/tests/test_isolation.py
[test_isolation_contexts_admin.py]: ../../backend/tests/test_isolation_contexts_admin.py
[test_isolation_contexts_guard.py]: ../../backend/tests/test_isolation_contexts_guard.py
[test_isolation_contexts_maintenance.py]: ../../backend/tests/test_isolation_contexts_maintenance.py
[test_isolation_contexts_scheduler.py]: ../../backend/tests/test_isolation_contexts_scheduler.py
[test_isolation_contexts_web.py]: ../../backend/tests/test_isolation_contexts_web.py
[test_isolation_contexts_webhook.py]: ../../backend/tests/test_isolation_contexts_webhook.py
[test_isolation_contexts_worker.py]: ../../backend/tests/test_isolation_contexts_worker.py
[test_jobs_api.py]: ../../backend/tests/test_jobs_api.py
[test_jobs_errors.py]: ../../backend/tests/test_jobs_errors.py
[test_jobs_events.py]: ../../backend/tests/test_jobs_events.py
[test_jobs_fence.py]: ../../backend/tests/test_jobs_fence.py
[test_jobs_runner.py]: ../../backend/tests/test_jobs_runner.py
[test_jobs_schema.py]: ../../backend/tests/test_jobs_schema.py
[test_jobs_stream.py]: ../../backend/tests/test_jobs_stream.py
[test_jobs_withings.py]: ../../backend/tests/test_jobs_withings.py
[test_jobs_worker.py]: ../../backend/tests/test_jobs_worker.py
[test_keks.py]: ../../backend/tests/test_keks.py
[test_keks_check.py]: ../../backend/tests/test_keks_check.py
[test_keks_files.py]: ../../backend/tests/test_keks_files.py
[test_keys_rewrap.py]: ../../backend/tests/test_keys_rewrap.py
[test_keys_wipe.py]: ../../backend/tests/test_keys_wipe.py
[test_log_chain.py]: ../../backend/tests/test_log_chain.py
[test_log_fail_closed.py]: ../../backend/tests/test_log_fail_closed.py
[test_log_redaction.py]: ../../backend/tests/test_log_redaction.py
[test_logging.py]: ../../backend/tests/test_logging.py
[test_mail.py]: ../../backend/tests/test_mail.py
[test_maint_privileges.py]: ../../backend/tests/test_maint_privileges.py
[test_metrics.py]: ../../backend/tests/test_metrics.py
[test_netguard.py]: ../../backend/tests/test_netguard.py
[test_oauth_idtoken.py]: ../../backend/tests/test_oauth_idtoken.py
[test_owner_filter.py]: ../../backend/tests/test_owner_filter.py
[test_queue_functions.py]: ../../backend/tests/test_queue_functions.py
[test_queue_guard.py]: ../../backend/tests/test_queue_guard.py
[test_queue_schedule.py]: ../../backend/tests/test_queue_schedule.py
[test_rls_guard.py]: ../../backend/tests/test_rls_guard.py
[test_role_check.py]: ../../backend/tests/test_role_check.py
[test_role_check_login.py]: ../../backend/tests/test_role_check_login.py
[test_role_check_refused.py]: ../../backend/tests/test_role_check_refused.py
[test_role_guard.py]: ../../backend/tests/test_role_guard.py
[test_roles_rerun.py]: ../../backend/tests/test_roles_rerun.py
[test_roles_script.py]: ../../backend/tests/test_roles_script.py
[test_session_guard.py]: ../../backend/tests/test_session_guard.py
[test_session_window.py]: ../../backend/tests/test_session_window.py
[test_totp_envelope.py]: ../../backend/tests/test_totp_envelope.py
[test_two_user_isolation.py]: ../../backend/tests/test_two_user_isolation.py
[test_user_context_guard.py]: ../../backend/tests/test_user_context_guard.py
[test_user_context_routes.py]: ../../backend/tests/test_user_context_routes.py
[test_user_context_types.py]: ../../backend/tests/test_user_context_types.py
[test_user_context_writers.py]: ../../backend/tests/test_user_context_writers.py
[test_web.py]: ../../backend/tests/test_web.py
[test_withings_client_logs.py]: ../../backend/tests/test_withings_client_logs.py
[test_withings_disconnect.py]: ../../backend/tests/test_withings_disconnect.py
[test_withings_disconnect_account.py]: ../../backend/tests/test_withings_disconnect_account.py
[test_withings_disconnect_routes.py]: ../../backend/tests/test_withings_disconnect_routes.py
[test_withings_fake_contract.py]: ../../backend/tests/test_withings_fake_contract.py
[test_withings_fake_generator.py]: ../../backend/tests/test_withings_fake_generator.py
[test_withings_oauth.py]: ../../backend/tests/test_withings_oauth.py
[test_withings_probe.py]: ../../backend/tests/test_withings_probe.py
[test_withings_probe_db.py]: ../../backend/tests/test_withings_probe_db.py
[test_withings_rate.py]: ../../backend/tests/test_withings_rate.py
[test_withings_reconcile.py]: ../../backend/tests/test_withings_reconcile.py
[test_withings_schema.py]: ../../backend/tests/test_withings_schema.py
[test_withings_schema_functions.py]: ../../backend/tests/test_withings_schema_functions.py
[test_withings_sync.py]: ../../backend/tests/test_withings_sync.py
[test_withings_tokens.py]: ../../backend/tests/test_withings_tokens.py
[test_withings_webhook.py]: ../../backend/tests/test_withings_webhook.py
[test_withings_webhook_callers.py]: ../../backend/tests/test_withings_webhook_callers.py
[test_worker_kinds.py]: ../../backend/tests/test_worker_kinds.py
[test_worker_withings_e2e.py]: ../../backend/tests/test_worker_withings_e2e.py
