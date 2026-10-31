# ADR 0003: Accounts, sessions, mandatory TOTP and step-up

Status: Accepted (2026-10-31, #4). Its two open questions were decided by the Architect on
2026-10-31 (§3 session lifetimes, §4 step-up readings).

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
| M7: export | M7 ADR | yes | PLAN §6 |

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
  `oauth_google_client_secret` secret file both exist. The web process's outbound allowlist holds
  Google's OIDC hosts only (#6). GitHub and Microsoft code, their re-auth caveats and their
  accepted risks are not ported.
- **Sign-up modes:** `invite` (default) and `closed`. **`open` is refused until the M8 gate**:
  the setting's validation rejects it, and the stored flag can only tighten what the environment
  allows (stricter wins, #12).
- **First admin:** `python -m ownvitals invite create`, then the owner signs up with an address
  in `OWNVITALS_ADMIN_EMAILS`; since TOTP is mandatory, the first sign-in goes straight to
  enrolment, before any admin route is reachable.

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
