# ADR 0011: Passkeys and QR approve-login

Status: Proposed (#177). Written before any M3 code, as ADR 0003 §10 promises. It fills the
"M3 ADR" cell of ADR 0003 §4's step-up table. Open questions for the owner are listed at the
end with the default that applies until they are answered.

## Context

PLAN §6 ("M3") adds two sign-in features under the M0 step-up rule:

| Addition | PLAN §6 |
|---|---|
| Passkeys | WebAuthn. "Use a phone" via QR (hybrid transport) gives standard QR sign-in |
| QR approve-login | Desktop QR with a 60 s single-use nonce bound to the desktop session cookie; the signed-in phone shows the desktop's browser, OS, IP and city and asks to approve; desktop receives the session via SSE |

"Passkey and QR-login management (add or remove a passkey, approve a login) follow the M0 step-up
rule." M3 is done when an Opus review passes and an e2e test covers each flow (PLAN §11).

ADR 0003 fixes what this builds on: account tables are system tables reached only through
`ownvitals.auth` (guard 8); **every account has TOTP and every sign-in completes it** before the
session yields a `UserContext` (§2); sessions rotate on every proof (§3); step-up is a TOTP or
recovery code within 10 minutes (§4); the client IP is the peer unless the peer is a trusted proxy,
and `CF-Connecting-IP` counts only from SWAG (§8); `/api/auth/*` bodies are never logged (§9).
Web may open no new outbound host (roadmap Amendment 1, ADR 0009). The accepted design shows
passkeys and QR approve as "Coming later" on the Security page, and the sessions list shows a place
and an IP per device (`docs/design/mockups/src/Security.dc.html`).

Several things are not fixed by the spec: the relying-party settings, where a passkey sits under
mandatory TOTP, whether a passkey counts as step-up, how "receives the session via SSE" works when
an SSE response can't set a cookie, where "city" comes from without a network call, and the
replay, race and phishing cases of the QR flow. This ADR decides each one with the safest
reversible default and names the test that proves it.

## Decision

### 1. Passkeys: relying party and ceremonies

- **Library.** Backend: `webauthn` (py_webauthn, Duo Labs, BSD-3-Clause), pinned to an exact
  version in `uv.lock`; it parses and verifies registration and authentication responses.
  The issue that adds it lists the licence of each new transitive dependency in
  `docs/dev/auth.md`; each is MIT, BSD, Apache-2.0, ISC or PSF, or the dependency is not added.
  Frontend: the browser's own `navigator.credentials` API, with a small base64url helper of our
  own; no WebAuthn package (the frontend licence list stays as it is). Only `ownvitals.auth`
  imports `webauthn` (a guard-8 style import test).
- **Relying party.** `rp.id` is the host of `OWNVITALS_PUBLIC_BASE_URL` (a derived setting,
  `webauthn_rp_id`, which may only be that host or a registrable suffix of it; startup refuses
  anything else). `rp.name` is "ownvitals". The **only accepted origin** is the origin of
  `OWNVITALS_PUBLIC_BASE_URL`; there is no list of extra origins and no related-origin file.
  A response whose `clientDataJSON.origin`, `type` or `challenge` differs is refused. Local
  development uses `http://localhost:<port>` with `rp.id` `localhost` (browsers allow WebAuthn on
  localhost only); `OWNVITALS_ENVIRONMENT=production` refuses a non-HTTPS origin.
  *Note:* moving the site to another host name before M8 (PLAN-operations §9.3) invalidates every
  passkey; users then sign in with password or Google plus TOTP and add a new one.
- **Registration options.** `attestation: "none"`; `authenticatorSelection.residentKey:
  "required"` and `requireResidentKey: true` (discoverable credentials, so sign-in needs no email);
  `userVerification: "required"`; no `authenticatorAttachment` (platform authenticators, security
  keys and a phone over the hybrid transport are all allowed); `pubKeyCredParams` ES256 (−7),
  EdDSA (−8), RS256 (−257) in that order; `excludeCredentials` lists the user's existing
  credentials; `timeout` 120 s.
- **User handle.** `user.id` is a per-user random 32-byte handle (`users.webauthn_handle`, unique,
  created at the first registration), **never** the user id or the email, so an authenticator
  holds no internal id. `user.name` and `user.displayName` are the account's email, which only
  appears in the user's own authenticator.
- **Authentication options.** Sign-in: `allowCredentials` empty (discoverable),
  `userVerification: "required"`, `timeout` 120 s; the account is found from the credential id,
  never from anything the client types, and the returned `userHandle` **must equal** that
  credential owner's `webauthn_handle`. Re-auth: `allowCredentials` lists only the session user's
  credentials that are not suspended, and the server checks the credential's owner is the session
  user. Sign-in options are rate-limited like the QR start: 20 per 10 minutes per IP (IPv6 by
  /64), at most one live `signin` challenge per pre-session (a new one replaces it).
- **Verification**, both ceremonies, in this order, every refusal the same generic answer (T86):
  the challenge (taken, purpose, binding), origin and type, the credential (known; owned by the
  `userHandle`'s account, or by the session user for re-auth; **not suspended**; its account
  active and, for sign-in, holding a confirmed TOTP, §3), then the `rp.id` hash, the UP **and UV
  flags set**, the algorithm on the allowed list, the signature, and last the sign count (§2). A
  suspended credential is refused before its signature is checked, so it can never move the
  counter. A missing UV flag is refused even if the authenticator claims to support it.
- **Challenges** are 32 random bytes, stored as SHA-256 in the existing `oauth_flows`-style pattern:
  a `webauthn_challenges` row with the purpose (`register`, `signin`, `reauth`), the session it is
  bound to (for `register` and `reauth` the user's session; for `signin` a pre-session cookie, §4),
  5 minutes' life, and taken by `DELETE … RETURNING` before verification, so each is single-use.
- **Hybrid transport** ("Use a phone"). Nothing on the server: the browser offers it because no
  `authenticatorAttachment` or `hints` restrict the ceremony. This is PLAN §6's "standard QR
  sign-in" and is separate from §5's QR approve-login. The e2e test can't scan a phone, so it is
  covered by the absence of a restriction (a unit test on the options) and the owner's manual
  check (Consequences).

Proved by: `test_passkey_options.py` (every option above, no attachment or hints, the user handle
is not the user id or email), `test_passkey_register.py` and `test_passkey_signin.py` (wrong
origin, wrong `rp.id` hash, wrong type, reused, expired or foreign challenge, UV flag clear, UP
flag clear, a disallowed algorithm, a bad signature: each refused; py_webauthn driven by a
software authenticator fixture in the tests, synthetic keys only).

### 2. Passkey storage and sign-count

- A new **system table** `webauthn_credentials` (account tables are system tables, ADR 0002 §3, and
  sign-in has to find a credential before it knows the user, which forced RLS on an owned table
  would forbid without a definer function). The issue's "owned table" wording is replaced by this
  for that reason; guard 8 keeps every access inside `ownvitals.auth`, and every read and write in
  a signed-in request filters on the session's user (404 for another user's id, as classbeat's
  account tables do).
- Columns: `id`, `user_id` (`ON DELETE CASCADE`), `credential_id bytea UNIQUE` (at most 1023
  bytes), `public_key bytea` (COSE, as returned), `sign_count bigint`, `transports text[]`,
  `aaguid uuid` (as reported; with attestation `none` it may be all zeros, and it is never trusted
  for anything but a display hint), `backup_eligible`, `backup_state`, `name` (the user's label,
  1 to 64 characters after NFKC, default "Passkey"), `created_at`, `last_used_at`,
  `suspended_at`.
- The public key is not a secret, so it is stored in clear (not under ADR 0004's envelope). Its
  integrity matters: an attacker who can write the table could add their own key, which is T24's
  SQL-injection case and the T60 host case, not a new one.
- At most **10 passkeys per account** (a setting); the eleventh registration is refused.
- **Synced passkeys** (backup-eligible) are allowed; the flags are stored and shown, not enforced.
- **Sign-count regression.** If the stored count or the new count is non-zero and the new count is
  not greater than the stored one, the assertion is refused, the credential is **suspended**
  (`suspended_at` set; it signs nothing until the user removes it and registers again), an
  `auth_events` row `passkey_suspended` is written and the account is mailed ("a passkey was
  refused and suspended"; no device details beyond its name). Both counts zero is normal for
  synced passkeys and accepted. A stored non-zero count followed by an assertion of 0 is a
  regression too and suspends the credential: intended as the strict default, although some
  authenticators reset or switch counter modes; such a passkey is removed and registered again.
  The count is updated by a conditional `UPDATE … WHERE sign_count =
  <read value>`, so two concurrent assertions with the same count can't both pass.

Proved by: `test_passkey_signcount.py` (regression suspends and mails; equal non-zero refused;
zero/zero accepted; two concurrent assertions with the same count: exactly one passes),
`test_passkey_limits.py` (the eleventh is refused), `test_auth_schema.py` (columns, cascade,
uniqueness), the same signcount test (stored 5, then 0: suspended), `test_auth_tables_guard.py`
(only `ownvitals.auth` touches the table).

### 3. Where a passkey sits under mandatory TOTP

- **A passkey replaces the first factor, not the second.** A passkey sign-in creates a session in
  exactly the state a password sign-in does: `mfa_at` empty, so it is not full, reaches only ADR
  0003 §2's allowlist, and becomes full only when a TOTP or recovery code is verified in it.
  "Every sign-in completes TOTP" (ADR 0003 §2) and the design's "Every sign-in asks for a code from
  it" stay true.
- **A passkey assertion counts as recent authentication**, not as step-up. A `reauth` ceremony
  (§1) sets `reauth_at` and rotates the session, like the password or a Google re-auth (ADR 0003
  §1). Step-up stays "a TOTP or recovery code within 10 minutes" (PLAN §6, ADR 0003 §4): a passkey
  never sets `stepup_at`.
- This is behind one setting, `passkey_satisfies_second_factor`, default **false**, which the
  settings validation refuses to set true until the owner decides (open question 1). With it
  false the code paths that would treat a passkey as a second factor don't exist: the setting is a
  placeholder for a later amendment, not a hidden switch.
- **Why this default.** It is the strictest reading of PLAN §6 and ADR 0003 §2 and is reversible: a
  later amendment can let a UV passkey make a session full (or count as step-up) without a data
  migration. The alternative (below) is stronger against phishing for users who never type a TOTP,
  so it is the owner's call.
- **Passkey sign-in and rate limits.** A failed assertion (unknown credential, bad signature,
  suspended credential, sign-count regression) answers one generic failure and counts in the
  sign-in failure buckets per IP and, once a credential maps to an account, per account (ADR 0003
  §1 "Rate limits"). An unknown credential id is indistinguishable from a bad signature (T9).
- **Disabled users and revocation.** A disabled account can't sign in with a passkey; deleting
  the account cascades.
- **Account recovery removes passkeys** (Architect decision, after the #212 review). `admin
  mfa-reset`, and any later recovery path that resets the second factor, also **deletes every
  passkey** of the account and expires every `pending`, `scanned` or `approved` `qr_logins` row it
  scanned or approved, in the same transaction as the TOTP removal; its mail says so. Otherwise a
  passkey an attacker registered while holding the account would survive the reset and win the
  first-enrolment race of ADR 0003 §2. And **a passkey sign-in to an account without a confirmed
  TOTP is refused** with the generic answer, so first enrolment after a reset is reachable only
  through the password set by the mailed reset link (ADR 0003 §2), never through a passkey.
- **Amendment (2026-11-03, Architect decision after the #226 review; amends ADR 0003 §2).**
  `admin mfa-reset` also **unlinks every Google identity** and **clears the password hash**, in
  the same transaction, so "only through the password set by the mailed reset link" holds
  literally: neither an identity nor a password that someone learned or linked while holding the
  account reaches the first enrolment. Its mail names what was removed (only what the account
  had) and carries the link that sets the new password. `keys wipe`, which also resets the second
  factor, removes passkeys and unlinks Google identities too, but keeps the password (a key loss,
  not a takeover); its mail points a Google-only account to the reset link. Lock order: every path
  that resets the second factor locks the account row `FOR UPDATE` first, and every passkey
  sign-in or registration locks it before its final write.
- **A password reset or change keeps passkeys**: it removes no second factor, so a planted passkey
  still meets the TOTP. Its mail lists the account's passkeys (names and creation dates) with a
  link to review them on the Security page.

Proved by: `test_auth_mandatory_totp.py` extended (a passkey sign-in yields a non-full session; the
route enumeration runs with it), `test_passkey_reauth.py` (`reauth_at` set, `stepup_at` not; a
`require_stepup` action after a passkey alone is refused `step_up_required`),
`test_passkey_signin.py` (one generic failure for every failure kind; a `userHandle` of another
account refused; a suspended credential refused without its counter moving; failures counted in
the buckets; sign-in options over 20 per 10 minutes per IP answer 429; another user's credential
refused at re-auth), `test_passkey_recovery.py` (after `admin mfa-reset`: no passkey row left, the
account's live QR rows expired, the mail says so; a passkey sign-in to an account without a
confirmed TOTP refused; a password reset keeps passkeys and its mail lists them),
`test_config.py` (the setting refuses true).

### 4. Pre-session cookie

Both the passkey sign-in and the QR flow need state in a browser that has no session yet. One
**pre-session cookie**, `__Host-ownvitals_pre`: 32 random bytes, SHA-256 stored, `HttpOnly`,
`Secure`, `SameSite=Strict`, `Path=/`, no `Domain`, 5 minutes' life, never read from a URL or another
header, never logged (added to ADR 0003 §9's list). It grants nothing on its own: it only binds a
challenge (§1) or a QR row (§5) to the browser that created it. **Every passkey sign-in start and
every QR start issues a new one** (replacing the old one and expiring what it bound), so its 5
minutes always outlive the longest QR flow (60 s + 120 s + 30 s = 3.5 minutes). A successful
sign-in deletes it. Its server state (`pre_sessions`: hash, created, expires) and
`webauthn_challenges` are deleted by the hourly auth purge (ADR 0003 §1) once expired.
CSRF for its writes is the double-submit cookie of ADR 0003 §1 plus the `Origin` check.

Proved by: `test_auth_presession.py` (attributes, life, deleted on sign-in, not accepted from a
header or query, a challenge or QR row from another pre-session refused; a start re-issues it),
`test_auth_purge.py` extended (expired pre-sessions and challenges deleted).

### 5. QR approve-login

**Roles.** The *desktop* is a browser without a session. The *phone* is a browser with a **full**
session of the user who approves. The desktop ends with a new session of that user.

**Flow.**

1. **Start.** The desktop calls `POST /api/auth/qr/start`. Web issues a new pre-session (§4) and a `qr_logins` row (system table, guard 8): `id`, `nonce_hash` (SHA-256 of a
   32-byte random nonce), `presession_hash`, `state` (`pending` → `scanned` → `approved` →
   `completed`; or `denied`, `expired`), `created_at`, `scan_deadline` (created + **60 s**),
   `desktop_ip`, `desktop_browser`, `desktop_os`, `desktop_place` (§6), and, once scanned,
   `approver_user_id`, `approver_session_id`, `scanned_at`, `approve_deadline` (scanned +
   **120 s**), `approved_at`, `complete_deadline` (approved + **30 s**). At most one `pending` or
   `scanned` row per pre-session: a new start (the desktop's "new code" button, or the 60 s
   refresh) expires the previous row in the same transaction. The response carries the row id and
   the QR content.
2. **The QR** encodes `https://<public host>/qr#<nonce>` (base64url). The nonce is in the
   **fragment**, so it never reaches a server, a proxy log or a `Referer`. The QR is rendered as an
   SVG server-side, as the TOTP QR is (ADR 0003 §1), dark on light in both themes (design README).
3. **Scan.** The phone's camera opens `/qr`; the SPA reads the fragment, removes it from the address
   bar and history (`history.replaceState`) and, only if the phone has a full session, calls
   `POST /api/auth/qr/scan` with the nonce in the body. Without a full session the SPA shows "Sign
   in on this phone first" and the nonce is dropped (never stored in the browser). Web takes the row
   by `UPDATE … SET state='scanned', approver… WHERE nonce_hash = $1 AND state='pending' AND now()
   < scan_deadline RETURNING …`: **the first scan wins**; a second scan, by any session, gets the
   one generic "this code has expired or was already used" answer. The response gives the desktop's
   browser, OS, IP and place, and the time of the request. Whoever scans first with any full
   session (a household member, someone who saw the screen) therefore learns that desktop's IP and
   place; accepted (T83): it is what the approver needs.
4. **Approve or deny.** The phone shows those details (§7), a code field and two buttons.
   Approve calls `POST /api/auth/qr/{id}/approve` **with a TOTP or recovery code in its body**,
   verified by that call for this row (§8): the step-up verifier (replay refused by `last_step`,
   recovery codes single-use, failures in the MFA buckets of ADR 0003 §2), but a step-up made
   earlier within the 10-minute window does **not** count, and the approval does not set the
   phone's `stepup_at`. Deny calls `POST /api/auth/qr/{id}/deny` (no code). Both are conditional
   updates that require `state = 'scanned'`, `approver_session_id` = the caller's session and
   `now() < approve_deadline`; the row records `approved_factor` (`totp` or `recovery_code`).
5. **The desktop learns the outcome over SSE.** `GET /api/auth/qr/{id}/events` is authorised only
   by the pre-session cookie matching the row (another pre-session or none: 404). It sends
   `scanned` (no details of the phone), `approved`, `denied` or `expired`, with keepalives every
   15 s, and closes at the row's last deadline (at most 3.5 minutes). **Bounded**: one live stream
   per row (a new request for the row closes the older stream), at most 5 open QR streams per IP
   (IPv6 by /64) and 100 per web process (settings; over either, 429). Wake-ups come from **one
   shared `LISTEN` connection per web process**, which hands notifications (the row id only, T27)
   to the waiting streams in memory; a stream holds no database connection while it waits and
   re-reads its row with one short query on a wake-up and every 5 s, so a lost notification only
   delays and open streams can't exhaust the pool. SWAG must not buffer this path (O6).
6. **Complete.** On `approved` the desktop calls `POST /api/auth/qr/{id}/complete`. Web takes the
   row by `UPDATE … SET state='completed' WHERE id=$1 AND presession_hash=$2 AND state='approved'
   AND now() < complete_deadline RETURNING approver_user_id`, creates a new session for that user
   and sets `__Host-ownvitals_session` **in this response**, deletes the pre-session cookie, and
   writes `auth_events` `qr_login_completed`. **Reading PLAN §6:** an SSE response has sent its
   headers before the approval happens, so it can't carry a `Set-Cookie`; and a session token in
   an SSE event body would be readable by script, against ADR 0003 §1's `HttpOnly` cookie rule. So
   "receives the session via SSE" means the SSE stream delivers the approval and the session cookie
   arrives on the immediately following same-origin `POST` bound to the same pre-session. The token
   never appears in an event, a body or a URL.

**The new desktop session.** It is **full**: `mfa_at` is the approval time, because a TOTP or
recovery code was entered **for this approval** (step 4). `reauth_at` and `stepup_at` are empty,
and it records `origin = 'qr_approval'`, the factor and the approving session's id.

**This amends ADR 0003 §2 and §7** (Architect decision, after the #212 review). ADR 0003 §2 reads
"full only once a TOTP or recovery code is verified in it"; from M3 a session is full when a
second factor was verified **in this session, or on its behalf for the one approval that created
it** (a code entered on the approving phone, bound to that QR row, within its deadlines). Nothing
else makes a session full. ADR 0003 §7's admin rule ("a TOTP in this session within 12 hours")
keeps its literal reading: a code entered on the phone for the approval does not count for the
admin role. `docs/dev/auth.md`'s "a sign-in never carries a second factor over from the browser's
old session" stays true: the factor is not carried over from an earlier session of that browser
but entered for this sign-in; the M3 QR issue adds a sentence saying so there. **Why a fresh TOTP
at approval counts while a passkey doesn't (§3):** the approval's factor is the mandatory TOTP
itself, entered for this login, only on another device; a passkey would replace that factor with a
different one, which is owner question 1.

Consequences: any sensitive or step-up
action on the desktop needs its own proofs; the **admin role does not count** until a TOTP or
recovery code is verified in the desktop session itself (ADR 0003 §7's "TOTP in this session"
holds literally); and a session born from a QR approval **can't approve** another QR login (no
chaining from a hijacked desktop). Its idle and absolute limits are the normal ones; it shows in
"Where you're signed in" as "approved from <phone browser> on <date>", and signing it out works as
for any session. After completing, the desktop shows **"Signed in as <email>"** before anything
else, so a desktop signed into the wrong account (someone else scanned and approved first, T83)
is visible at once; the victim's own later scan was refused with the generic answer.

**Account state.** Approve, scan and complete each re-check that the approver's account is active
and the approving session still exists; a session revoked between approve and complete (log out
everywhere, a password change) makes `complete` fail with the generic answer.

**Purge.** Rows are deleted one hour after their last deadline by the hourly auth purge (ADR 0003
§1), with expired `pre_sessions` and `webauthn_challenges` (§4); `desktop_ip` and `desktop_place`
never outlive that.

Proved by: `test_qr_login.py` (the happy path; deny; each deadline at, just before and just after;
scan without a full session refused; complete without, or with another, pre-session refused;
complete before approval refused; the desktop session's flags: full, no `reauth_at`, no `stepup_at`,
admin not counted, cannot approve a QR login), `test_qr_login_factor.py` (approve without a code
refused even with a step-up 1 minute old; a wrong code refused and counted; a replayed TOTP step
refused; a recovery code works once; the phone's `stepup_at` unchanged), `test_qr_login_races.py` (two concurrent scans: one
wins; two concurrent completes: one session; approve and deny concurrently: one outcome; approve
after expiry refused; a revoked approver session makes complete fail), `test_qr_login_sse.py`
(events in order, 404 for a foreign pre-session, no detail of the phone and no token in any event,
closes at the deadline, works with NOTIFY suppressed; a second stream for a row closes the first;
the sixth stream from one IP answers 429; open streams hold no pooled connection), e2e `qr-login.spec.ts` (two browser
contexts: desktop shows the QR, the "phone" context posts the nonce it reads from the rendered QR's
data attribute in test mode only, approves with a TOTP, the desktop lands signed in).

### 6. City: where "place" comes from

PLAN §6 asks for the desktop's city; web may open no new outbound host. One setting,
`geo_source`, with three values; the **default is `none`**:

| Value | Source | Trust rule |
|---|---|---|
| `none` (default) | nothing; the place is shown as "Place unknown" | — |
| `cloudflare` | Cloudflare's visitor-location headers `CF-IPCity` and `CF-IPCountry` (the "Add visitor location headers" managed transform) | read **only** on a request whose peer is in `cf_connecting_ip_proxies` (the same rule as `CF-Connecting-IP`, ADR 0003 §8), each header present exactly once; otherwise ignored |
| `geoip_file` | a local GeoIP database file (MaxMind DB format), path in a setting, mounted read-only, refreshed by the owner | a lookup of the client IP of ADR 0003 §8; no network call; a missing or unreadable file means "Place unknown" and one warning in the log |

- Whatever the source, the place is **display text only**: at most 64 characters after NFKC,
  control and bidirectional-override characters removed, rendered as text by React (T55). It is
  "City, CC" or "CC" or "Place unknown". It never takes part in any security decision, rate limit
  or audit key.
- The same place feeds the sessions list's "Place and IP" (the Security mock-up) and is kept only
  as long as the session IP (30 days, ADR 0003 §1 "Purge").
- `geoip_file` needs a reader library (`maxminddb`, Apache-2.0) and a database whose licence the
  owner accepts; **until the owner answers question 2 the settings validation refuses
  `geoip_file`**, and a later reviewed PR adds the library. `cloudflare` needs the owner's Cloudflare transform and SWAG to strip the two headers
  from requests that did not come from Cloudflare (open item O17).
- **Why `none`.** No outbound call, no new file, no dependency, nothing an attacker can choose;
  the QR approval stays safe without a place because the place is not a security signal anyway
  (§7). The owner picks the source (open question 2).

Proved by: `test_geo_source.py` (`none` shows "Place unknown"; `cloudflare` honours the headers
only from a listed peer, refuses a duplicate header, strips control characters and caps the length;
`geoip_file` reads a synthetic test database (built in the test from documentation addresses
192.0.2.0/24 and 2001:db8::/32) and falls back to unknown when the file is missing; no outbound
connection in any mode, under the egress hook).

### 7. What the phone shows, and approval fatigue

- The approval screen shows: the desktop's browser and OS "as reported by that browser", its IP,
  its place (§6), the time of the request, and the sentence **"Only approve if you are signing in
  on a computer in front of you right now. ownvitals never asks you to scan a code it sent you."**
- When the desktop's IP differs from the phone's current IP (with IPv6 compared by /64), the
  screen adds **"This computer is on a different network than your phone."** The household case
  (one NAT) shows no warning; a phone on mobile data does, which is the intended nudge, not a
  block.
- **Browser and OS** come from the desktop's `User-Agent` through a small parser of our own
  (families: Firefox, Chrome, Edge, Safari, Opera, Samsung Internet, other; Windows, macOS, iOS,
  iPadOS, Android, Linux, ChromeOS, other). The raw header is never stored; the parsed names are.
  An attacker controls this header, which is why the screen says "as reported".
- **No push.** Nothing is ever sent to the phone unasked: an approval starts only from a scan
  made on the phone. Push-bombing, the usual approval-fatigue attack, has no channel.
- **Limits per approver** (fixed windows, ADR 0003 §1 "Rate limits", buckets HMAC-ed): scans 10
  per 10 minutes per account; approvals **3 per hour** per account; over the limit, 429 with
  `Retry-After`. Desktop starts: 20 per 10 minutes per IP (IPv6 by /64), at most one live row per
  pre-session.
- **Every completed approval is mailed** to the account ("A new sign-in was approved from your
  phone": browser, OS and place of the new device, a link to "Where you're signed in"; no health
  data, T59) and listed in "Recent security activity".

Proved by: `test_qr_login.py` (the different-network flag with IPv4 and IPv6 /64; the UA parser on
a fixed list of synthetic strings; the raw UA not stored), `test_auth_ratelimit.py` extended (each
QR bucket at, below and over its limit), `test_auth_email.py` extended (the completed-approval mail:
no token, nonce or health data; link from the public base URL).

### 8. Step-up and recent authentication: ADR 0003 §4's M3 row

| Action | Recent auth (10 min) | Step-up (10 min) | Why |
|---|---|---|---|
| Add a passkey | **yes** | **yes** | Adds a sign-in method, like linking Google (ADR 0003 §4) |
| Remove a passkey | **yes** | **yes** | Same as unlinking Google; a stolen session must not remove the owner's method and lock them out |
| Rename a passkey | — | — | A label only; CSRF and the user filter as for any write |
| Approve a QR login | — | **a TOTP or recovery code entered with the approval itself**; the 10-minute window does not count | PLAN §6; it signs in a new device and makes its session full (§5). Recent authentication (the first factor again) adds no possession proof |
| Scan or deny a QR login | — | — | Neither grants anything |

`require_sensitive` covers the first two rows (it exists since #11); the fourth is the approve
call's own code check (§5 step 4). The recent-authentication proof may itself be a passkey `reauth` ceremony (§3), a password or
Google re-auth. Every action writes an `auth_events` row; a refusal too.

Proved by: `test_auth_stepup.py` extended, one pair of tests per row (refused without the proof,
accepted within the window), keeping the list equal to this table; e2e `passkey.spec.ts` (Chromium
with a CDP virtual authenticator, `WebAuthn.addVirtualAuthenticator` with `hasUserVerification` and
`isUserVerified` true: add a passkey with step-up, sign out, sign in with the passkey, complete the
TOTP, remove the passkey with step-up).

### 9. Audit and logging

- New `auth_events` kinds: `passkey_registered`, `passkey_removed`, `passkey_renamed`,
  `passkey_signin`, `passkey_signin_failed` (with `detail.reason` from a closed set), `passkey_reauth`,
  `passkey_suspended`, `qr_login_scanned`, `qr_login_approved`, `qr_login_denied`,
  `qr_login_completed`, `qr_login_expired` (written when a scanned row expires; an unscanned row
  has no user to attribute it to). They hold kinds, ids, the IP and the place, never a nonce,
  challenge, credential id, public key, user handle, pre-session or session token.
- Added to the never-logged list (ADR 0003 §9): the pre-session cookie, WebAuthn request and
  response bodies (`clientDataJSON`, `attestationObject`, `authenticatorData`, signatures), QR
  nonces and the `/qr` fragment. The QR SSE path carries only the row id.

Proved by: `test_auth_logging.py` extended: it plants a nonce, a challenge, a credential id, a user
handle and a pre-session cookie through the real routes and greps the captured logs and
`auth_events` for each.

### 10. Frontend

- Security page: the "Passkeys" and "Approve sign-ins from your phone" rows of the mock-up, with
  the shared step-up dialog for add and remove; the approval screen has its own code field (§5). The sign-in page gains "Sign in with a
  passkey" and "Sign in with your phone" (the QR). A screen the mock-ups don't cover (the phone's
  approval screen, the desktop's QR panel) goes to the Architect before it is built (AGENTS.md "UI
  design"); this ADR fixes only its wording above.
- The CSP stays `'self'`: the QR is a server-rendered SVG, WebAuthn needs no third-party script.

## Alternatives

- **A UV passkey satisfies the second factor** (session full after a passkey alone). Phishing-
  resistant, and a passkey with user verification combines possession with a PIN or biometric;
  NIST SP 800-63B treats such an authenticator as multi-factor. Not the default because ADR 0003 §2
  and the design say every sign-in asks for the TOTP, and because a synced passkey's security is the
  sync account's (an Apple or Google account), which ownvitals can't assess. Kept as open question
  1 behind `passkey_satisfies_second_factor`.
- **A passkey as the second factor after a password.** Stronger than TOTP against phishing, but a
  user with a passkey would then never use their TOTP, which PLAN §6 makes mandatory, and lost
  passkeys would fall back to TOTP anyway. More states for little gain.
- **A passkey counts as step-up.** PLAN §6 defines step-up as a fresh TOTP; it would also let a
  stolen unlocked phone with a synced passkey step up. Left for the owner with question 1.
- **Owned table for passkeys** (the issue's wording). Sign-in must look up a credential before any
  `UserContext` exists; under forced RLS that needs a `SECURITY DEFINER` lookup, a new reviewed
  definer for no gain over guard 8. Rejected.
- **Attestation `direct` with an AAGUID allowlist.** Would let us refuse unknown authenticators, at
  the price of a metadata service (a new outbound host) and privacy prompts. Not needed for a
  household; rejected.
- **The session token over SSE.** Breaks `HttpOnly`; rejected (§5).
- **A short code typed on the phone** (number matching). It counters push-bombing, which this flow
  doesn't have; against a phishing page that relays the QR, the page relays the code too. The
  warning text and the network flag are the counter; rejected.
- **City by an IP-geolocation web API.** A new outbound host in web; refused by roadmap Amendment
  1 and ADR 0009.
- **Desktop QR without a pre-session cookie** (the nonce alone). Anyone who sees the QR on screen
  (shoulder-surfing, a screen share) and scans it first would not get the session, but anyone who
  learns the row id could complete it; the cookie binding is what PLAN §6 asks for.

## Consequences

- The M3 passkey issue adds `webauthn`, the `webauthn_credentials` and `webauthn_challenges` tables,
  `users.webauthn_handle`, the pre-session cookie, the passkey routes, the Security page rows and
  `passkey.spec.ts`. It is security-critical (Opus Developer and Reviewer).
- The M3 QR issue adds `qr_logins`, its routes, the SSE stream, the UA parser, `geo_source` with
  `none` and `cloudflare` (`geoip_file` waits for the owner's answer and a reviewed dependency),
  the approval screen and `qr-login.spec.ts`. Security-critical.
- Both extend `test_auth_mandatory_totp.py`'s allowlist: the anonymous routes (passkey sign-in
  start and finish, QR start, events and complete) and nothing reachable by a non-full session
  beyond ADR 0003 §2's list.
- `docs/dev/auth.md` gains a "Passkeys and QR approve-login" section from those issues.
- The hybrid transport ("Use a phone") is not exercised in CI; the owner's check on real devices is
  a manual step of the M3 done-when, recorded in `docs/status.md` (never INTEGRATION_VERIFIED by an
  agent).
- A host-name change before M8 invalidates every passkey (§1); the M8 plan for the move says so.
- Threat model: section J (T76 to T89), O6 extended to the QR SSE path, open items O16 and O17.

## Open questions

**For the owner** (each has a default that applies meanwhile; the Architect records them in
`docs/status.md` "Blocked on the owner"):

1. **Does a passkey with user verification satisfy the second factor, or count as step-up?**
   Default meanwhile: **no to both** (§3); a passkey replaces the password only, TOTP is asked at
   every sign-in, and step-up stays TOTP or a recovery code. Safe-off: `passkey_satisfies_second_factor`
   stays false and refuses to be set. A "yes" is a roadmap amendment (it reads PLAN §6's "TOTP
   mandatory" differently) and an amendment to this ADR and ADR 0003 §2.
2. **Where does the city come from?** `cloudflare` (needs the Cloudflare visitor-location transform
   and SWAG stripping the headers from non-Cloudflare peers, O17), `geoip_file` (which database and
   whether its licence and update duty are acceptable), or nothing. Default meanwhile: **`none`**,
   "Place unknown" everywhere. Safe-off: `none`.
3. **Is the QR approve-login wanted for non-household users at M8?** It is the one flow in which a
   user can hand a session to a device controlled by someone else (T79). Default meanwhile: on for
   household accounts; the M8 human security review re-decides it. Safe-off: a setting
   `qr_login_enabled` (default true in the household phase) that hides the start route and refuses
   scans when false.

**Decided by the Architect (2026-11-03, this ADR).** RP id and the single origin; attestation
`none`, UV required, discoverable credentials, no attachment restriction; a random user handle;
system table under guard 8; sign-count regression suspends; 10 passkeys per account; add and remove
need recent auth and step-up, approve needs a code entered for that approval (amending ADR 0003
§2 and §7); account recovery deletes passkeys; the pre-session cookie; the SSE-then-POST
reading of "via SSE"; the deadlines (60 s scan, 120 s approve, 30 s complete); first scan wins; no
chaining from a QR-born session; the admin role not carried by a QR approval; the approval screen's
wording and the network flag; the limits of §7.

## Amendment (2026-11-03): deviations of the QR login implementation (#179, merged as #232)

Recorded by the Architect from the implementation notes in
[docs/dev/auth-qrlogin.md](../dev/auth-qrlogin.md), which hold the detail. The decisions above
stand except where this section says otherwise; status stays Proposed.

- **Section 4, the pre-session.** A start does not always issue a pre-session that replaces the
  old one and expires what it bound. While the old pre-session is live, a start gives a new token
  in the same row (rotated in place) and each kind replaces only its own binding: a new passkey
  sign-in challenge replaces the old one, a new QR start expires the previous QR row. The old
  token is dead at once. Reason: tabs share the cookie jar, so a passkey start in another tab must
  not cancel a QR login (the #165 item for this ADR). An expired or unknown pre-session is still
  deleted with what it bound.
- **Section 5, the binding.** `qr_logins.presession_id`, a foreign key to `pre_sessions` with
  `ON DELETE SET NULL`, replaces `presession_hash`. It is the same binding (the cookie resolves
  to the row) and it survives the in-place rotation, which a stored hash would not.
- **Section 5, "Account state".** `approver_session_id` is `ON DELETE SET NULL`, so a rotation of
  the approving session (a step-up on the phone during the window), not only a revocation, also
  ends the flow: `complete` needs the approving session still to exist. This is stricter than the
  ADR; the phone scans a new code.
- **Section 9, audit kinds.** Two kinds are added to the list: `qr_login_started` (no user; the
  desktop's IP and the row id) and `qr_login_refused` (`detail.action`: `scan`, `approve`, `deny`
  or `complete`). The issue asks for an event on start and section 8 asks for a row on every
  refusal. Neither holds a nonce, a code, a cookie or a token.
- **Test names.** The proofs named `test_qr_login*.py` and `test_geo_source.py` are
  `test_auth_qrlogin*.py` (the auth suites' naming).
