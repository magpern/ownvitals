# ADR 0003: Accounts, sessions, mandatory TOTP and step-up

Status: Accepted (2026-10-31, #4); amended 2026-10-31 by #49 and #72, 2026-11-08 by #247 and
2026-11-12 by #475 (TOTP optional for users, roadmap Amendment 13; see "Amendments"). Its
two open questions were decided by the Architect on 2026-10-31 (§3 session lifetimes, §4
step-up readings). Post-port review (#16, 2026-11-03): no decision changed; `fake-oidc` runs from
the e2e overlay `compose.e2e.yaml` rather than a compose profile, still only with
`OWNVITALS_ENVIRONMENT=test` (threat model T13, gap G2).

## Context

PLAN §6 fixes M0 identity: classbeat auth (password, Google OIDC, sessions, rate limits,
notices, purge, audit), **TOTP mandatory for every account**, protected sessions (rotation, idle
and absolute limits, log out everywhere) and **step-up 2FA, a fresh TOTP within 10 minutes**,
for every sensitive action that exists before real data arrives. The real-data gate (§9.2)
needs mandatory 2FA before any real Withings account connects.

classbeat ADR 0003 is the base. It has password, Google, GitHub and Microsoft sign-in,
server-side sessions, CSRF, rate limits and recent authentication; TOTP is optional for users
and gates only the admin role; step-up is a TOTP within 5 minutes. What changes here, and why:

- **A password alone must never reach health data.** A taken-over account discloses
  special-category data, so every account has TOTP and every sign-in completes it before the
  session yields a `UserContext` (§2).
- **Step-up** is a TOTP or recovery code within 10 minutes, for the PLAN §6 list on top of
  classbeat's (§4).
- **Google is the only OAuth provider** (PLAN §3); GitHub and Microsoft are not ported.
- **TOTP seeds use the envelope scheme of ADR 0004 from the first enrolment** (§5).
- **Trusted proxies are SWAG and Cloudflare**, not Caddy (§8).
- **Open sign-up is refused until the M8 gate** (D7).

The port (#10) keeps classbeat's optional TOTP so its diff stays reviewable; #11 adds mandatory
TOTP, session protection and step-up; #14 tests the flows end to end.

## Decision

### 1. Ported unchanged from classbeat (#10)

The design and tests of these parts are classbeat ADR 0003 at the pinned commit, with the names
changed (`classbeat` → `ownvitals`) and Google as the only provider; §2 to §9 below say what
else changes.

| Area | classbeat ADR 0003 | Key points |
|---|---|---|
| Account tables | §1 | System tables (ADR 0002 §3) reached only through `ownvitals.auth` (guard 8); user-scoped queries and 404 for another user's ids; `users`, `identities` (provider `google` only), `sessions`, `known_devices`, `pending_signups`, `auth_tokens`, `oauth_flows`, `invites`, `mfa_totp`, `mfa_recovery_codes`, `rate_limits`, `auth_events`; every token stored as a hash, passwords as argon2id |
| Verified email only | §1, §5 | An account exists only after a mail link proves the address (the password typed again at confirmation) or Google states `email_verified`; no unverified-account state |
| Purge | §1 | Hourly in the worker: expired sessions and flows, used tokens, old invites, unused known devices (90 days), `auth_events` and session IPs after 30 days |
| Request to context | §2 | One dependency, `current_context`, from the `__Host-ownvitals_session` cookie; anonymous → 401; missing level → 403 with a machine-readable reason. Extended in §2 below |
| Cookies and tokens | §3 | 32 random bytes, SHA-256 stored; `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, no `Domain`; never from a URL or another header |
| CSRF, origin, redirects | §4 | `Origin` equals `OWNVITALS_PUBLIC_BASE_URL` (or `Sec-Fetch-Site: same-origin`); `X-CSRF-Token` = `HMAC(csrf_key, session.id)` or the double-submit cookie; no sign-in from a mail link; `Cache-Control: no-store`; `return_to` relative only |
| Passwords | §5 | argon2id (19 MiB, t=2, p=1), rehash on parameter change; 12 to 128 characters after NFKC; one generic failure, a dummy verification for unknown emails |
| Sign-up and mail tokens | §5 | Pending sign-ups, invite consumed in the creating transaction; tokens single-use, in the URL fragment, 24 h / 30 min / 1 h; email change confirmed in the requester's session, old address notified; links built from the public base URL; mail sent after the response |
| Google OIDC | §5 | Authlib with state in `oauth_flows` (state, PKCE S256, nonce, 10 min, taken by `DELETE … RETURNING` before the code exchange); ID token RS256 only, `iss`, `aud`/`azp`, `exp`, `iat`, `nonce`, JWKS cache rules; identity key `(google, sub)`, never the email; the unverified-email hand-off bound to the browser; re-auth with `max_age=0` and `auth_time` within 5 minutes |
| Recent authentication | §6 | `reauth_at` within 10 minutes, by password or a Google `reauth` flow; each rotates the session |
| Admins | §7 | `users.role`; the admin role counts only with a TOTP in this session within 12 hours; bootstrap by `invite create`, `OWNVITALS_ADMIN_EMAILS` and `admin grant`/`revoke`; `admin mfa-reset` |
| TOTP and recovery codes | §7 | RFC 6238 SHA-1, 6 digits, 30 s, ±1 step, 160-bit secret; QR as a server-rendered SVG; one code confirms; concurrent first enrolments: one wins; replay refused by a conditional update of `last_step`; 10 recovery codes of 10 base32 characters, HMAC-stored, each single-use |
| Derived keys | §8 | `app_secret_key` (secret file) → HKDF-SHA256 (salt `ownvitals-v1`): `csrf`, `rate-limit`, `device`, `recovery-codes`, `totp-pending`. The `totp-v1` key is not ported (§5) |
| Rate limits | §9 | Fixed windows in `rate_limits`, bucket keys HMAC-ed; reserve first, release on success; failures-only for sign-in and MFA; IPv6 by /64; bounded argon2 pool (503 after 5 s); 429 with `Retry-After`; no lockout; the limits table unchanged |
| Logging | §10 | Never logged: session, device, CSRF and OAuth cookies, `Authorization`, `X-CSRF-Token`, OAuth query strings, `/api/auth/*` bodies; `auth_events` hold kinds, never secrets |
| Tests and fakes | §11 | In-process OIDC fake for every bad-token case; `fake-oidc` in e2e under the `test` profile, configurable only with `OWNVITALS_ENVIRONMENT=test` |

### 2. Mandatory TOTP and the second-factor gate (#11)

- **Every account has a confirmed TOTP, and every sign-in completes it.** A sign-in by password
  or by Google creates a session with `mfa_at` empty. The session is **full** only once a TOTP or
  recovery code is verified in it (or a first enrolment is confirmed in it), which sets `mfa_at`
  and `stepup_at` and rotates the session (§3).
- `current_context` issues a `UserContext` **only for a full session** (ADR 0002 §1). A session
  that isn't full reaches only this allowlist, and everything else answers
  403 `two_factor_required`:
  - `GET /api/auth/session`, which says what is missing (`verify` or `enrol`) and returns the CSRF
    token;
  - TOTP and recovery-code verification, when the user has a confirmed TOTP;
  - TOTP enrolment (start, confirm), when the user has none;
  - sign-out; `/api/health` and `/api/ready`.

  A test enumerates every route the app registers and fails on any route outside the allowlist
  that a non-full session can reach (#11 `test_auth_mandatory_totp.py`), so a new route can't
  skip the gate.
- **A non-full session is short-lived.** Its first factor counts as recent authentication for
  the 10-minute window of §1; a session still not full after that is deleted and the user signs
  in again. A stolen password therefore buys at most ten minutes of verify-or-enrol pages, never
  data. No new setting: it reuses the recent-authentication window.
- **Google sign-in** goes through the same gate; so does the first sign-in after a sign-up, which
  goes straight to enrolment.
- **Admins:** unchanged rule (TOTP within 12 hours, classbeat §7). Since every full session
  passed TOTP at sign-in, an admin re-verifies every 12 hours of a longer session.
- **First enrolment.** Before it, whoever holds the password and enrols first owns the factor:
  the exposure classbeat accepted for new admins now applies to every new account, between the
  sign-up confirmation and the first enrolment (normally the same sitting). Sign-up confirmation
  needs both the mailbox and the password (classbeat §5), and every enrolment or replacement is
  mailed to the account's address (classbeat's enrolment notice). The threat model lists this as
  a proposed accepted risk for the owner to confirm.
- **Lost authenticator:** a recovery code completes the sign-in and satisfies step-up (§4), so
  the user can reset TOTP; or the owner runs `python -m ownvitals admin mfa-reset <email>`, which
  removes the TOTP, recovery codes and known devices, revokes every session, writes an
  `auth_events` row and mails a reset link; the next sign-in goes to enrolment.
- Failed codes use classbeat's MFA buckets (5 per 5 min per account and IP, 20 per hour per
  account, 20 per 5 min per IP); five failures in an hour mail the user.

### 3. Session protection (#11)

- **Rotation** (a new token in a new row, the old row deleted, the absolute expiry kept):
  on sign-in, on completing the second factor, on re-authentication, on step-up, and on any
  privilege change. A request in flight with the old token gets 401; the SPA repeats only safe
  requests, once (classbeat §3).
- **Idle and absolute limits** are settings: the idle limit defaults to **72 hours** (classbeat:
  7 days) and the absolute limit to **30 days** (Architect decision, 2026-10-31). The shorter
  idle limit suits a household computer shared by several people. Every request refuses an
  expired session, deletes it and clears the cookie.
- **Revocation:**
  - a password or email change revokes every other session of the user;
  - a reset, a role change, `admin mfa-reset` and removing or resetting TOTP revoke all of them
    (on TOTP removal the current session drops back to enrolment, §4);
  - sign-out deletes the row; **log out everywhere** (step-up first) deletes every session of the
    user, the current one included.
- Known devices (classbeat §3) stay: they only exempt the user from the per-account sign-in cap.

### 4. Step-up (#11)

- **Definition:** `stepup_at` within the **step-up window, 10 minutes** (a setting; PLAN §6).
  **A recovery code counts as a fresh second factor**, as in classbeat (Architect decision,
  2026-10-31): otherwise a user who lost the authenticator could not reset 2FA without the
  owner. It is set by any TOTP or recovery code verified in the session: at sign-in, at
  enrolment, or through `POST /api/auth/mfa/stepup` (classbeat's route). Each sets `stepup_at`, rotates the
  session and writes an `auth_events` row (a failure too); failures count in the MFA buckets of
  §2.
- **Refusal:** 403 `step_up_required`; the SPA shows one reusable step-up dialog and retries the
  action (#11).
- **One dependency** (`require_stepup`), exported for other packages (#13's connect and
  disconnect). The actions and what each needs:

| Action | Recent auth (10 min) | Step-up (10 min) | Source |
|---|---|---|---|
| Connect, reconnect or disconnect Withings (seam in #13, provider in M1) | yes | yes | PLAN §6; classbeat §6 |
| Delete account | yes | yes | PLAN §6; classbeat §6 |
| Change password, set a first password, change email | yes | yes | PLAN §6; classbeat §6 |
| Link or unlink Google | yes | yes | classbeat §6 |
| Replace the TOTP (a new authenticator), remove or reset 2FA, regenerate recovery codes | — | yes | PLAN §6; classbeat §6 |
| Log out everywhere | — | yes | PLAN §6 |
| First TOTP enrolment (no confirmed factor) | the sign-in itself (§2) | — (no factor yet) | Architect decision |
| Admin writes (#12) | — | yes, within **5 minutes** | classbeat ADR 0011 §6 |
| M3: add or remove a passkey, approve a QR login | M3 ADR | yes | PLAN §6 |
| M7: export (request) | yes | yes | PLAN §6; ADR 0013 §4 (amended 2026-11-11, #394) |
| M7: doctor PDF (request) | — | yes | PLAN §6; ADR 0013 §6 (amended 2026-11-11, #394) |
| M7: download of an export or doctor PDF | — | yes, and a same-origin page | ADR 0013 §4 |

- **Reading PLAN §6 under mandatory TOTP** (Architect decision, 2026-10-31):
  - **"disable 2FA"** means removing the factor: the user's other sessions are revoked and the
    current one drops back to enrolment (`mfa_at` cleared), so there is never a full session
    without TOTP;
  - **"enable 2FA"** needs step-up only when it replaces a confirmed authenticator; a first
    enrolment, with no factor to step up with, is protected by the fresh sign-in instead.
- #11 tests each action twice: refused without a fresh code, accepted within the window
  (`test_auth_stepup.py`, one test per action, the list matching PLAN §6).

### 5. TOTP seeds on the envelope scheme

- **TOTP seeds are encrypted with ADR 0004's envelope from the first enrolment**: AES-256-GCM
  under a per-write data key, payload AAD `ownvitals-totp-v1|<user_id>`, data key wrapped under the
  current KEK with AAD `ownvitals-totp-dek-v1|<key_id>|<user_id>` (#8, #10
  `test_totp_envelope.py`). `keys rewrap` and `keys wipe` cover them (ADR 0004 §3).
- classbeat's interim form (`key_id = 'app-v1'`, a key derived from `app_secret_key`) and its
  one-time `keys rewrap --totp` migration are not ported: #8 merges before #10, so no seed is ever
  written in the old form.
- A pending enrolment secret is not stored: it is sealed for the client for 10 minutes under the
  HKDF key `totp-pending`, AAD `<user_id>|totp-pending` (classbeat behaviour).
- A seed that does not decrypt fails MFA closed with the generic error and does not count as a
  failure: `totp_key_unavailable` (the operator restores the KEK file) or `totp_decrypt_failed`
  (fixed by `admin mfa-reset`), as classbeat ADR 0004 §2.
- Rotating `app_secret_key` invalidates CSRF tokens, rate-limit buckets, known devices and
  **recovery codes**; with TOTP mandatory, every user must then regenerate recovery codes (under
  step-up). The runbook says so; it does not touch TOTP seeds.

### 6. Sign-up and providers

- **Google is the only OAuth provider.** It is on only when its client id setting and the
  `oauth_google_client_secret` secret file both exist. GitHub and Microsoft code, their re-auth
  caveats and their accepted risks are not ported.
- **Web's outbound connections** are exactly those of
  [roadmap Amendment 1](../roadmap.md): Google's OIDC hosts for sign-in (#6), the configured SMTP
  server, for the account mails, which run in the request as in classbeat, and for the outbox
  mail that a background task in the web process sends (ADR 0007; amended 2026-10-31, #72),
  and, from M1, the one Withings authorization-code exchange (ADR 0004 §7). Nothing else; #6
  and #22 test both processes' lists.
- **SMTP never from the worker** (Architect decision, amended 2026-10-31, #49 and #72). Mail
  goes out through one mail module, from web and from the operator CLI (for example the notices
  of the KEK or seed wipe, which makes no Withings call, from its one-off container; ADR 0004
  §3), never from the worker, which never opens SMTP. **Mail that originates in the worker**
  (the finding notifications of threat model T59 and O8; owner notices such as PLAN §4.3's)
  goes through the **mail outbox of [ADR 0007](0007-mail-outbox-and-owner-notices.md)**: its
  rows name a fixed kind and a recipient, never an address or free text, and web sends them
  through the same guard-listed mail module and SMTP server. The notifications issue (M6/M7)
  builds the outbox; until then, in M1, PLAN §4.3's owner notice is in-app only (ADR 0007, ADR
  0004 §6).
- **Sign-up modes:** `invite` (default) and `closed`. **`open` is refused until the M8 gate**:
  the setting's validation rejects it, and the stored flag can only tighten what the environment
  allows (stricter wins, #12).
- **First admin:** `python -m ownvitals invite create`, then the owner signs up with an address
  in `OWNVITALS_ADMIN_EMAILS`; since TOTP is mandatory, the first sign-in goes straight to
  enrolment, before any admin route is reachable. As in classbeat, an account exists only with a
  verified email (Google's address is used only when Google marks it verified), so the list
  grants the role only to an address its holder has proved (threat model T75).

### 7. Rate limits in the household phase

classbeat's limits stand. In the household phase every member sits behind one NAT, so the per-IP
buckets are shared by the whole household; the per-(account, IP) and per-account buckets keep
one member's failures from locking out another. The threat model lists this as a proposed
accepted risk.

### 8. Client IP and trusted proxies

- The client IP is the peer address, unless the peer is in `OWNVITALS_TRUSTED_PROXIES` (default:
  loopback only). On the home server that list is SWAG (192.168.1.151): web then takes the
  right-most `X-Forwarded-For` entry that is not a trusted proxy, and honours `CF-Connecting-IP`
  **only** on a request whose peer is SWAG (#10). uvicorn's own proxy-header handling is off.
- The host firewall admits port 8096 only from SWAG (§9.2), so nothing reaches web around it.
- SWAG must set or overwrite `CF-Connecting-IP` only for connections that come from Cloudflare's
  address ranges, so a client reaching SWAG directly can't choose its rate-limit bucket. That is
  the owner's SWAG configuration (M2), listed in the threat model.

### 9. Logging and audit

- classbeat §10's never-logged list, plus TOTP codes, recovery codes and the bodies of step-up
  and enrolment requests. `auth_events` also record each enrolment and removal of the second
  factor, each step-up and failed step-up, and each log-out-everywhere; they hold kinds and ids,
  never codes, secrets or health data. A redaction test plants each kind of secret and
  greps the captured logs (#10 `test_auth_logging.py`).

### 10. Later (not M0)

- **M3:** passkeys (WebAuthn, with the hybrid QR transport) and QR approve-login, each under the
  step-up rule; their own ADR.
- **M7:** export requires step-up.
- **M8:** the breached-password check (it needs a new outbound host), the per-account
  denial-of-service limit and the session lifetimes are revisited in the human security review.

## Alternatives

- **Optional TOTP (classbeat):** rejected by PLAN §6; a phished or reused password would reach
  health data.
- **Email or SMS one-time codes:** the mailbox is already the reset path, so it would not be a
  second factor; SMS needs a new provider and outbound host and is weaker than TOTP.
- **Step-up by password only:** proves the first factor again, not possession of the second; a
  stolen password plus a stolen session would pass.
- **TOTP-only re-authentication for everyone** (classbeat allows it for admins): one proof would
  cover both rows of the table in §4; keeping two proofs for the riskiest actions costs one extra
  prompt.
- **Passkeys as the M0 second factor:** a larger port and new browser flows; M3 adds them.
- **Stateless sessions (JWT):** can't be revoked, so "log out everywhere" and idle expiry need a
  table anyway (classbeat).

## Consequences

- #10 ports classbeat's auth with Google only and envelope-encrypted TOTP; #11 adds the gate of
  §2 (route-enumerating test), the session rules of §3 and the step-up table of §4 with one test
  per action, plus the SPA's forced enrolment and step-up dialog; #14 runs sign-up → enrolment →
  sign-in with TOTP, Google with TOTP, a step-up action and a password reset end to end.
- #13's connect and disconnect routes depend on #11's `require_stepup`.
- Threat model T1 to T18 cite this ADR.

## Amendments

### 2026-11-12 (#475): the authenticator app is optional for users, required for admins

Roadmap Amendment 13 (owner, 2026-11-12: "authenticator app should be optional, not
mandatory"). It replaces §2's first rule ("Every account has a confirmed TOTP, and every sign-in
completes it"), §4's "Reading PLAN §6 under mandatory TOTP" and, for accounts without TOTP, §4's
"Definition" and ADR 0011 §3's "a passkey never sets `stepup_at`". Everything else in §1 to §9
stands; for an account **with** a confirmed TOTP nothing changes.

**Who must have a TOTP.** `second_factor_policy.totp_required`: the stored role is `admin`, or
the normalized address is in `OWNVITALS_ADMIN_EMAILS` (the next sign-in would grant the role).
Everyone else may go without. The rule is a pure module; `session_resolve` reads the role and the
address with the session and passes the setting in.

**When a session is full** (`SessionInfo.full`, the one place the rule lives in Python, and
`session_full_sql.owes_no_second_factor`, the same rule in SQL for the purge, the live-session
list and the disconnect job's re-check):

| Account | Full when | Otherwise owes |
|---|---|---|
| a confirmed TOTP (any role) | `mfa_at` is set in this session (unchanged) | `verify` |
| no TOTP, required (admin) | never: it must enrol first | `enrol` |
| no TOTP, not required (user) | always, after the first factor | — |

A session that owes something still reaches only §2's allowlist and lives ten minutes; the route
enumeration (`test_auth_second_factor_gate.py`, which replaces `test_auth_mandatory_totp.py`) runs
for the `verify` and the `enrol` kinds, and checks that the gate refuses no route to a user
without TOTP. When a user enrols, every other session of the account, which passed no code, owes
`verify` at once and ends after its ten minutes: the account then behaves exactly as one that
always had TOTP. An `mfa_at` whose TOTP is gone never makes an admin full, and the admin role
(`SessionInfo.role`) also needs a confirmed TOTP. The SessionInfo default is `totp_required=True`,
so a caller that does not say fails closed.

**Step-up without TOTP.** For an account with no confirmed TOTP that is not required to have one
(`second_factor_policy.first_factor_steps_up`), a fresh first factor sets `stepup_at` as well as
`reauth_at`, rotates the session once and writes a `step_up` row with `detail.method`
(`password`, `google`, `passkey`), besides the existing `reauth` or `passkey_reauth` row:
- the password re-authentication (`POST /api/auth/reauth`);
- a Google re-authentication, under the 2026-11-08 (#247) rule unchanged: only an account
  without a password, a callback within 5 minutes of the start, a verified email, no future
  `auth_time`; every refusal sets nothing (fail closed);
- a passkey re-authentication (ADR 0011 §1's `reauth` ceremony), so ADR 0011 §3 now reads "a
  passkey never sets `stepup_at` **for an account with TOTP**".

`stepup_first_factor.steps_up` reads the account under the user-row lock that each of the three
paths already holds before its rotation, so a TOTP confirmed or a role granted meanwhile is seen,
and the first factor is then a recent authentication only. The window is still
`stepup_window_minutes` (10). `require_stepup`, `require_sensitive` and `require_admin_stepup` are
unchanged: they read `stepup_at`. An admin never steps up by the first factor (it has a TOTP or is
not full), so the admin writes keep a TOTP within 5 minutes.

**Turning TOTP off** (`DELETE /api/auth/mfa/totp`, `require_stepup`): for an account with TOTP the
step-up is a TOTP or recovery code. It removes the TOTP and the recovery codes, revokes every
other session, writes `totp_removed`, mails the new `totp_removed` notice (en and sv) and rotates
this session with `mfa_at` cleared: a user's stays full, an admin's goes back to enrolment.

**Unchanged on purpose (provisional, for the M8 review):**
- a passkey **sign-in** to an account without a confirmed TOTP is still refused (ADR 0011 §3), so
  a user without TOTP signs in with the password or Google; the passkey serves as step-up. Allowing
  it is a reversible later change;
- a QR approve-login needs a TOTP or recovery code entered with the approval (ADR 0011 §8), so an
  account without TOTP cannot approve one (`SessionInfo.may_approve_qr` now says so);
- `admin mfa-reset` and `keys wipe` behave as before; the reset's first enrolment is reachable
  only through the mailed reset link for an admin; a user then simply has no TOTP.

**Risk.** PLAN §6's "a password alone must never reach health data" no longer holds for a user who
declines TOTP: a stolen password (or a live Google session) reaches that user's own data, and the
step-up adds a fresh first factor, not a second one. In particular the walk-up case of the
2026-11-08 amendment (someone at an unlocked device with the victim's ownvitals and Google
sessions) now passes step-up for a Google-only account without TOTP, since Google's answer without
a fresh `auth_time` is the step-up there. That is the owner's decision; it is limited to accounts
whose owner chose no TOTP, never an admin (cross-user power) and never an account with TOTP. The
Security page recommends TOTP while it is off, and the threat model's entries for these cases go
to the owner at the M8 review.

**Frontend.** The session answer gains `totp_required`; the SPA routes from `mfa` (full) first.
A user without TOTP is offered enrolment once per browser session after signing in, with "Skip for
now"; an admin is not. The "Confirm it's you" dialog asks an account without TOTP for the
password, Google or a passkey alone. The Security tab gains the "Second factor" card.

**Tests.** `test_auth_second_factor_gate.py` (the rule, pure and end to end, every route for each
kind of account, the purge), `test_auth_stepup_without_totp.py` (password, Google, passkey, each
with its refusal; accounts with TOTP and admins never step up by the first factor; the disconnect
job's re-check), the changed tests named in the PR, the frontend's `access`, `guards`,
`twoFactorPages` and `securityOptionalTotp` tests, and the e2e `auth-password`, `auth-google` and
`step-up` specs' no-TOTP paths.

### 2026-11-11 (#394): the §4 table rows for M7

Made in place above and marked "amended 2026-11-11, #394": the "M7: export" row now says what
ADR 0013 §4 and §6 decided and `reports/export_routes.py` and `reports/doctor_routes.py` do: the
export request needs recent authentication and a step-up (`require_sensitive`), the doctor PDF
request a step-up only (`require_stepup`), and a download of either a step-up on a same-origin page.

### 2026-10-31 (#49): follow-ups of the #41 review

- §6: SMTP never from the worker. Mail goes out through one mail module, from web and from the
  operator CLI's one-off containers; mail that originates in the worker goes through an outbox
  that web sends, and PLAN §4.3's owner notice is in-app plus mail (Architect decisions). The
  earlier text called the mail module a web-only path.

### 2026-10-31 (#72): follow-ups of the #58 review

- §6: the outbox is ADR 0007's: a row names a fixed kind and a recipient, never an address or
  free text, and web's background task sends it through the same mail module and SMTP server,
  which web's outbound connections now name. In M1, PLAN §4.3's owner notice is in-app only;
  mail through the outbox starts with the notifications issue (M6/M7). The earlier text had the
  M1 notice mailed through the outbox (Architect decisions).

### 2026-11-08 (#247): Google re-authentication without `auth_time`; every refused callback logged

Roadmap Amendment 7, wave 1 (H1). It replaces "re-auth with `max_age=0` and `auth_time` within 5
minutes" in the §1 table (Google OIDC) and "by password or a Google `reauth` flow" in the
recent-authentication row, for Google.

**Context.** ownvitals ported classbeat at `d806051`, before classbeat fixed this (classbeat ADR
0003 §5, amendments of 2026-11-03, #522, and 2026-11-06, #687). On classbeat's live stack every
Google re-authentication ended `reauth_failed` `{"why": "auth_time"}`: Google ignores `max_age=0`
and the OIDC `claims` request and returns an ID token without a fresh `auth_time` from a live
Google session. Its discovery document lists no `auth_time` in `claims_supported` and does not
advertise `claims_parameter_supported`. In ownvitals the same rule makes every Google
re-authentication fail, so an account without a password cannot pass `require_sensitive` (§4)
through Google at all. The reproduction is `tests/test_auth_oauth_reauth.py`'s first test: its fake
now behaves like the live Google (`FakeProviders.honours_claims_request = False`).

**Options classbeat weighed (#522)**, and why (a) is not a proof:
- (a) `prompt=login` plus an `iat` within seconds, bound to the flow's nonce and PKCE. Google does
  not document `prompt=login` (only `none`, `consent`, `select_account`), and nothing in its answer
  shows a provider honoured it. A fresh `iat` shows only that the token was minted now, which
  Google does silently from any live Google session: exactly what the walk-up case has. State,
  nonce and PKCE bind the answer to this browser, which is that same browser.
- (b) Google re-authentication only with a fresh `auth_time`, else the password: with Google never
  sending one, a Google-only account would have no way through but a password reset.
- (c) An email code or link: not built; it would need its own decision.

**Decision** (classbeat's #687 split by account kind, adopted unchanged in substance):
- **An account with a password never re-authenticates through Google.** `POST
  /api/auth/oauth/google/start` with `purpose=reauth` answers 403 `reauth_use_password` and
  creates no flow; the callback refuses it again under the user row lock (`reauth_use_password`,
  `why` `has_password`, a `reauth_failed` event), whatever `auth_time` Google sends, so a password
  set between the start and the callback gains nothing. Such an account confirms with its password
  (or a passkey, ADR 0011).
- **An account without a password** is re-authenticated by Google's answer **without a fresh
  `auth_time`**, as the answer to this flow only: a `reauth` flow of this session's user, single
  use (taken before the code exchange), state, PKCE and nonce verified, the ID token checked as for
  any sign-in (`iss`, `aud`/`azp`, `exp`, `iat`), its `sub` the Google identity linked to this
  user, `email_verified` true (else `invalid`, `why` `email_unverified`), and the callback at most
  **5 minutes** after the start by the database clock (else `reauth_expired`, `why` `flow_old`; the
  flow itself still lives 10 minutes). An `auth_time` that is present must not lie more than 60 s
  in the future (`reauth_stale`, `auth_time_future`); a present but old one is accepted.
  `prompt=login` is not sent and plays no part.
- **Unchanged:** Google's authorization request keeps `max_age=0` and the `claims` request
  (harmless; a fresh `auth_time` is still accepted). The numbers (300 s, 60 s) are not widened.
  **Step-up is untouched:** a recent authentication never sets `stepup_at`, every action that needs
  recent authentication also needs a TOTP or recovery code within the 10-minute step-up window
  (§4, `require_sensitive`), and TOTP stays mandatory (§2).
- The rule is the pure module `auth/oauth_reauth_policy.py`; `oauth_outcomes._reauth` locks the
  user row, checks the identity and applies it.

**Does mandatory TOTP change the answer?** It makes classbeat's accepted residual risk smaller, not
larger, so the decision stands without a new owner question. In classbeat a walk-up attacker
(someone at an unlocked device with the victim's classbeat and Google sessions) could pass recent
authentication through Google and, while TOTP was unconfirmed, enrol their own TOTP. In ownvitals:
- a Google `reauth` flow starts only from a **full** session (`require_user`), which has already
  passed a confirmed TOTP (§2); a session still owing enrolment or a code cannot start one;
- every action that needs recent authentication also needs a fresh second factor (§4), which
  Google never gives;
- a first 2FA activation never sits behind a Google re-authentication: it follows the sign-in
  itself (§2, §4 "First TOTP enrolment"), and after a Google sign-in Google's missing `auth_time`
  plays no part (the e2e `google-reauth` spec runs it with fake-oidc sending none).

So the walk-up attacker gains recent authentication and nothing it unlocks without the victim's
authenticator. That residual is accepted on the same terms as classbeat's owner decision of
2026-11-06; accounts with a password are not exposed to it at all.

**Logging.** Every callback that does not end `ok` writes one `WARNING` line
`oauth callback refused: provider=… purpose=… outcome=… why=…` (`ownvitals.auth.oauth_routes`).
`why` is a fixed word (`no_flow`, `provider_mismatch`, `state_mismatch`,
`provider_error=<an RFC 6749 or OIDC error code, else unrecognized>`, `no_code`, `no_session`,
`other_user`, `provider: <the failed check>`, `has_password`, `flow_old`, `email_unverified`,
`auth_time_future`, `not_linked_to_this_user`, `no_invite`, …). It never holds a code, state,
nonce, token, query string, email or claim value; claim names only. The outcome code stays the
page's reason (`/account?oauth=reauth_use_password`, `reauth_expired`, `reauth_stale`); wording it
is the frontend's (G4, #250).

**Consequences.** `tests/test_auth_oauth_reauth.py` (the rule end to end), `test_oauth_reauth_policy.py`
(the table), `test_auth_oauth_logging.py` (one line per refusal, nothing secret) and the e2e
`google-reauth` spec. Commands and codes: [docs/dev/auth-reauth.md](../dev/auth-reauth.md). The
frontend still offers "Use Google instead" to a password account until G4 words
`reauth_use_password`; the start refuses it, so nothing is weakened meanwhile.

