# Legal drafts: claim sources

DRAFT support file for the owner's legal gate (issue #419, ADR 0016 item 6). Each factual claim in
`privacy`, `terms`, `security` and `cookies` (en and sv) with where the code or an ADR shows it.
"Owner" means the text is a placeholder or an operational fact that no committed file proves; the
owner must confirm it. Nothing here is approved.

| # | Claim (page) | Source |
|---|---|---|
| 1 | Withings data is fetched through the public API at the user's request; not endorsed by Withings (privacy 2, terms 4) | AGENTS.md "Security rules" (public API only, D1); PLAN D1 |
| 2 | Account data: email, password hash, Google identity key `(google, sub)` not the email, TOTP and recovery-code hashes, known devices, sessions, auth events (privacy 3) | ADR 0003 table "Account tables" (`users`, `identities`, `mfa_totp`, `mfa_recovery_codes`, `known_devices`, `auth_events`) |
| 3 | Passwords argon2id; recovery codes HMAC-stored and single-use; TOTP mandatory (privacy 3, security) | ADR 0003 table (argon2id; 10 recovery codes, single-use); `docs/adr/0003` §1 "Mandatory TOTP" |
| 4 | Passkeys and QR approve-login supported (security) | ADR 0011 |
| 5 | Birth year and sex stored for score bands; day assignment (privacy 3) | ADR 0013 §export list ("profile (birth year, sex for score bands, day assignment, display settings)") |
| 6 | Withings data stored: devices, measures, payloads, sleep, activity, workouts, ECG with signal, findings, scores (privacy 3) | ADR 0013 export list; PLAN §4 and derived tables (`findings`, `scores`) |
| 7 | Withings tokens encrypted at rest, AES-256-GCM with KEK rotation (privacy 9, security) | PLAN §3 module table (`crypto/envelope.py`, ADR 0004) |
| 8 | Only Withings (`wbsapi.withings.net`, `account.withings.com`) and, if AI enabled, `api.anthropic.com`, from the worker; web makes Google sign-in calls (`www.googleapis.com` JWKS) and one code exchange (privacy 5, security) | `backend/src/ownvitals/egress/core.py:39-49`; `egress/__init__.py:18-19`; AGENTS.md "Security rules" |
| 9 | Sign-in events and invitations purged after 30 days; known devices after 90; session IPs after 30 (privacy 6) | `backend/src/ownvitals/auth/purge.py:70-71,146-163` |
| 10 | Session idle 72 h, absolute 30 days (privacy 6, security, cookies) | `backend/src/ownvitals/config_auth.py:28-29` |
| 11 | Export download as JSON and CSV with step-up; link expires after 24 h; expired record kept 90 days (privacy 6-7) | ADR 0013 §export row ("ready + 24 h", "kept 90 days"), ADR 0003 §4 table ("M7: export (request) step-up yes") |
| 12 | Disconnect revokes at Withings first, optional deletion of imported data; account delete revokes then purges; failure told to the user (privacy 7) | `docs/dev/connections.md` "Withings disconnect and account deletion (#163)"; `auth/purge.py` docstring; PLAN §4.3 |
| 13 | Delete account, disconnect, export, credential changes and AI consent need step-up (security, privacy 7) | ADR 0003 §4 table; ADR 0014 §3 |
| 14 | AI off by default: needs operator gates (model id, provider review, eval, flags) and per-user explicit consent with step-up; caps default 0 (privacy 8) | ADR 0014 §1 (gates 3-5) and §3; `config_ai.py:11,19-20,35-41` |
| 15 | What is sent to the model (summaries, daily values) and what never is (tokens, email, name, ids, raw payloads, ECG signal, times of day) (privacy 8) | ADR 0014 tool table and "Never returned by any tool" |
| 16 | Withdrawing AI consent stops use and deletes conversations and digests (privacy 6, 8) | ADR 0014 §3; PLAN §7.4 |
| 17 | AI conversations deleted after 90 days; AI audit log 12 months (privacy 6) | ADR 0014 §5 "Retention"; `config_ai.py:24,29` |
| 18 | AI answers labelled "AI-generated; can be wrong; not medical advice" (privacy 8, terms 2) | ADR 0014 §5 "Output" |
| 19 | Health data is special-category data; explicit consent Art. 9(2)(a); privacy notice, DPIA, processor agreements, transfer check, 72 h breach procedure are M8 gate items (privacy 4-5, security) | PLAN-operations §9.3; PLAN D7; ADR 0016 item 6 |
| 20 | No analytics, ads, tracking, third-party content; public pages set no cookies, no JavaScript (cookies, privacy 5) | ADR 0016 §1, §5 (Decision, "no Set-Cookie", "No analytics"); `public_site.py` |
| 21 | Cookie names, attributes (`__Host-`, Secure, no Domain, HttpOnly/SameSite) and lifetimes (cookies) | `backend/src/ownvitals/auth/cookies.py:11-27,33-85` (session 30 d via `session_absolute_days`; device 365 d; csrf 30 d, JS-readable, SameSite Strict; oauth 10 min; pending 24 h; pre 5 min) |
| 22 | Theme choice in localStorage `ownvitals.theme` (cookies) | `frontend/src/theme/theme.ts:5,17,27-38` |
| 23 | Row-level security enforces per-user ownership (security) | AGENTS.md "Security rules" (owner_id, forced RLS); ADR 0002 |
| 24 | Logs never contain measurement values, tokens, webhook secrets (security) | AGENTS.md "Health data" (log-redaction tests) |
| 25 | Webhooks verified and only a trigger to fetch (security) | AGENTS.md "Security rules"; PLAN §4.4; ADR 0004 |
| 26 | Findings tiered, some experimental; not medical advice, not Withings-endorsed (terms 2, 4) | PLAN D9; ADR 0016 item 6; ADR 0015 health-wording rule |
| 27 | Mail contains no health measurements (privacy 5) | NOT VERIFIED in this PR: `auth/mails.py` and `mail_texts/` not audited line by line; finding-notice mail content (#316) must be checked by the owner/Architect |
| 28 | Transport encryption (TLS) for health data (privacy 9) | PLAN-operations §9 (SWAG/Cloudflare, SSL Full strict) as planned; deployment is owner-run, so the statement is an owner confirmation |
| 29 | Backups: nightly encrypted dump to another machine (privacy 5-6) | PLAN-operations §9.1 table; retention period and provider are owner placeholders |
| 30 | Hosting, email provider, processor agreements, controller identity, contacts, authority, liability, governing law | OWNER placeholders; no code or ADR fixes them |
| 31 | Signup limited to invite/closed until M8 (terms 1) | `docs/dev/admin.md` signup mode table (`open` refused until M8) |
