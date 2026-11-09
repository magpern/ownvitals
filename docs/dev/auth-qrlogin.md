# Auth: QR approve-login

How a signed-in phone approves a sign-in on a desktop: [ADR 0011](../adr/0011-passkeys-and-qr-login.md)
sections 5 to 9. #179 adds the backend; the desktop's QR panel, the phone's approval screen and
`qr-login.spec.ts` come with the frontend issue (#180). Passkeys are in
[auth-passkeys.md](auth-passkeys.md), the rest of the account system in [auth.md](auth.md).

## The pieces

| Path | What |
|---|---|
| `backend/migrations/versions/0015_qr_login.py` | the system table `qr_logins` (guard 8; `db/tables.py` lists it) and four columns on `sessions`: `origin`, `mfa_by_approval`, `approved_factor`, `approved_by_session_id` |
| `backend/src/ownvitals/auth/qrlogin.py` | the rows and their state machine: start, complete, expiry (the desktop's side) |
| `backend/src/ownvitals/auth/qrlogin_phone.py` | the phone's transitions: scan, approve, deny |
| `backend/src/ownvitals/auth/qrlogin_model.py` | the windows, states and values (pure); `qrlogin` re-exports them |
| `backend/src/ownvitals/auth/qrlogin_routes.py` | `/api/auth/qr`, the desktop's routes, and the router lifespan that runs the stream hub |
| `backend/src/ownvitals/auth/qrlogin_phone_routes.py` | `/api/auth/qr`, the phone's routes: scan, approve, deny |
| `backend/src/ownvitals/auth/qrlogin_stream.py` | the desktop's SSE stream and its limits |
| `backend/src/ownvitals/auth/useragent.py` | the browser and OS families of section 7, from `User-Agent` |
| `backend/src/ownvitals/auth/geo.py` | the place of section 6 (`geo_source`) |
| `backend/tests/qrtools.py` | the desktop and phone browsers of the tests (`QrBrowser`, `phone`, `fresh_code`) |

## Routes

| Route | Needs | Does |
|---|---|---|
| `POST /api/auth/qr/start` | nothing (20 per 10 minutes per IP) | a new pre-session token and a request: `{id, qr_svg, qr_url, expires_in: 60}` |
| `GET /api/auth/qr/{id}/events` | the pre-session cookie the row is bound to | SSE: `scanned`, then `approved`, `denied`, `expired` or `completed`, then the stream closes; 404 for another pre-session's row, 429 over the stream limits, 204 for a reconnect whose `Last-Event-ID` is the end it already saw |
| `POST /api/auth/qr/{id}/complete` | that pre-session cookie, the row approved | the new session's cookie in this response; `{id, email, role}` so the desktop shows "Signed in as …" first; mails the account |
| `POST /api/auth/qr/scan` | a full session not born from a QR approval (10 per 10 minutes per account) | `{nonce}`: the first scan wins; `{id, desktop: {browser, os, ip, place}, requested_at, different_network, expires_in}` |
| `POST /api/auth/qr/{id}/approve` | the session that scanned it (3 per hour per account, and the MFA buckets) | `{code}`: a TOTP or recovery code entered for this approval |
| `POST /api/auth/qr/{id}/deny` | the session that scanned it | ends the request |

Every refusal of a code or row that is unknown, expired, used, or another session's or
pre-session's is the same 404 `qr_unavailable`. A wrong code is 400 `invalid_code` and an
`mfa_failed` with `detail.purpose` `qr_approve`. A session born from a QR approval gets 403
`qr_approval_not_allowed` at scan and approve, before the nonce is looked at. With
`qr_login_enabled` false, start and scan answer 404 `not_found`.

## Decisions the code makes

- **The new desktop session** is full: `mfa_at` is the approval time, because a code was entered
  on the phone for this approval (ADR 0011 section 5, amending ADR 0003 sections 2 and 7).
  `reauth_at` and `stepup_at` are empty. `origin` is `qr_approval` for the life of the session
  (every rotation carries it), so it can never approve a QR login. `mfa_by_approval` keeps the
  phone's code from counting for the admin role (`SessionInfo.role`); a TOTP verified in the
  desktop session itself (`/api/auth/reauth/totp`, which sets `mfa_at`) clears it. A step-up on
  the desktop does not.
- **Approve's own code.** Order: the account row (`FOR SHARE`), the row's state (no code is
  checked for a row that can't be approved, so nothing is spent or counted), the code (the
  step-up verifier: a TOTP step once, a recovery code once), the conditional update. An earlier
  step-up does not count, and the approval neither sets the phone's `stepup_at` nor rotates its
  session. The approvals limit counts every attempt.
- **Lock order** (the #165 item for ADR 0011): `approve` and `complete` lock the account row
  first, as a passkey sign-in does; `reset_mfa` locks it `FOR UPDATE`, expires every live row the
  account scanned or approved, then revokes its sessions. A `complete` that waited for a reset
  finds its row expired; one that committed first has its session revoked by the reset
  (`test_auth_qrlogin_races.py`).
- **Bindings.** The row holds the pre-session's id (`SET NULL` when the pre-session goes) and,
  once scanned, the approving user (`CASCADE`) and session (`SET NULL`). `complete` needs the
  approving session still to exist, the account active and its TOTP confirmed. A live row whose
  pre-session or approving session is gone, or whose deadline passed, is set `expired` when the
  stream next reads it, at the next start of that pre-session, or by the hourly purge;
  `qr_login_expired` is written for a row that was scanned.
- **The pre-session across tabs** (the #165 item for ADR 0011). Every passkey sign-in start and
  every QR start gives a new pre-session token, but in the **same row** while the old one is
  live, so a passkey sign-in started in one tab does not cancel a QR login in another, nor the
  reverse. Each kind replaces only its own: a new sign-in challenge replaces the old one, a new
  QR start expires the previous QR row. The old token is dead at once (`presession.issue`).
- **"Receives the session via SSE"** reads as section 5 step 6 says: the stream delivers the
  approval and the cookie arrives on the following same-origin `POST …/complete`. No token is ever
  in an event, a body or a URL. The events carry the state only, never anything about the phone.
- **Stream limits** (T89): one stream per row (a new one closes the older), `qr_streams_per_ip`
  per IP (IPv6 by /64) and `qr_streams_per_process` per process. Wake-ups come from one
  `LISTEN qr_logins` connection per web process: a second `jobs.hub.EventHub`, on its own
  channel. With the jobs hub, two pooled connections are held for the life of the process. A
  stream holds no connection while it waits and re-reads its row on a wake-up and every 5 s;
  shutdown (`app.state.qr_hub`, closed from `__main__.Server.handle_exit`) ends every stream.
- **Behind SWAG** the response says `X-Accel-Buffering: no` and `Cache-Control: no-store`; the
  owner's SWAG location for `/api/auth/qr/` must not buffer either (threat model O6).
- **The rate-limit keys** use the client IP of ADR 0003 section 8 (`request_ip`): behind no
  trusted proxy, `X-Forwarded-For` and `CF-Connecting-IP` change nothing. Per pre-session limits
  would limit nothing (a new pre-session is free), so the per-IP limit is the bound.
- **Browser and OS** come from our parser (`useragent.py`): Firefox, Chrome, Edge, Safari,
  Opera, Samsung Internet or other; Windows, macOS, iOS, iPadOS, Android, Linux, ChromeOS or
  other. The raw header is never stored.
- **Place.** `geo_source=none` (default): "Place unknown", nothing read, no call. `cloudflare`:
  `CF-IPCity` and `CF-IPCountry`, only from a peer in both `trusted_proxies` and
  `cf_connecting_ip_proxies`, each exactly once; header bytes are read as UTF-8 when they are,
  then NFKC, control and format characters (bidirectional overrides among them) removed, at most
  64 characters; the country must be two letters or digits. `geoip_file` is refused until the
  owner answers ADR 0011 open question 2. The place is display text only. #179 does not add it to
  the sessions list ("Place and IP" of the Security mock-up); that comes with the frontend.
- **Admin bootstrap.** A QR `complete` does not run the `ADMIN_EMAILS` grant (it would revoke
  the approving phone's session); a password, Google or passkey sign-in does.

- **A code spent on a lost race stays spent.** If a concurrent approval or a deny takes the row
  between approve's code check and its update, the TOTP step or recovery code that passed stays
  used (`qrlogin_phone.approve`'s docstring). A recovery code lost that way must be replaced from the
  Security page like any used one.
- **Switching `qr_login_enabled` off** hides start and refuses scans; `approve`, `deny`, the
  stream and `complete` of rows already in flight still finish (at most 3.5 minutes).

## Where this differs from ADR 0011 (for the Architect's amendment)

| ADR 0011 | Here | Why |
|---|---|---|
| §4: every start issues a new pre-session, "replacing the old one and expiring what it bound" | while the old pre-session is live, a start gives a new token **in the same row**; each kind replaces only its own binding (a new `signin` challenge, a new QR row); the old token is dead at once; an expired or unknown pre-session is still deleted with what it bound | the #165 item (c): tabs share the cookie jar, so a passkey start in one tab cancelled a QR login in another |
| §5: `qr_logins.presession_hash` | `qr_logins.presession_id` (FK to `pre_sessions`, `ON DELETE SET NULL`) | the same binding (the cookie resolves to the row), and it survives the in-place rotation above, which a stored hash would not |
| §5 "Account state": a session **revoked** between approve and complete makes complete fail | `approver_session_id` is `ON DELETE SET NULL`, so a **rotation** of the approving session (a step-up on the phone during the window) ends the flow too | stricter; the phone scans a new code |
| §9: the list of `auth_events` kinds | adds `qr_login_started` (no user; the desktop's IP and the row id) and `qr_login_refused` (`detail.action`) | the issue asks for an event on start; §8 asks for a row on every refusal. Neither holds a secret |
| §5 to §7 "Proved by": `test_qr_login*.py`, `test_geo_source.py` | `test_auth_qrlogin*.py` (table under "Tests") | the auth suites' naming |

## Frontend (#180)

- **Desktop** (`components/QrLoginPanel.tsx`, "Sign in with your phone" on the sign-in page): the
  server's SVG through `QrCode`, then the stream (`api/qrlogin.ts` `watchQrLogin`, an
  `EventSource`). After `approved` it posts `complete` and shows "Signed in as <email>" with
  "Continue", which only then reads the session. An unscanned code also expires on the page's
  clock after `expires_in`; "Show a new code" starts a new request.
- **Phone** (`pages/ApproveLoginPage.tsx`, `/qr`): the fragment is read once and removed with
  `history.replaceState` before anything else; without a full session the page says "Sign in on
  this phone first" and never scans. The first scan is the only one the page sends. "Approve…"
  opens a dialog with the look and wording of "Confirm it's you", whose code goes in the approve
  call's body (section 8: a step-up does not count). The server's "Place unknown" is shown in the
  page's language.
- e2e: `e2e/specs/qr-login.spec.ts`.

## Settings

| Setting | Default | |
|---|---|---|
| `qr_login_enabled` | `true` | ADR 0011 open question 3's safe-off: false hides start and refuses scans |
| `geo_source` | `none` | `none` or `cloudflare`; `geoip_file` is refused |
| `qr_streams_per_ip` | `5` | 1 to 5 |
| `qr_streams_per_process` | `100` | 1 to 100 |

## Audit kinds

`qr_login_started` (no user; the desktop's IP), `qr_login_scanned`, `qr_login_approved`
(`detail.factor`), `qr_login_denied`, `qr_login_completed` (`detail.session`), `qr_login_expired`
(a scanned row only: an unscanned one has no user), `qr_login_refused` (`detail.action`: `scan`,
`approve`, `deny` or `complete`; not in the ADR's list, the refusal row of section 8). They hold
row ids, the IP and the place: never a nonce, the QR URL, a code, a cookie or a token
(`test_auth_qrlogin_logging.py`).

## Tests

ADR 0011 names its proofs `test_qr_login*.py` and `test_geo_source.py`; they are:

| ADR 0011 | Here |
|---|---|
| `test_qr_login.py` | `test_auth_qrlogin.py` |
| `test_qr_login_factor.py` | `test_auth_qrlogin_factor.py` (with the rate limits of section 7) |
| `test_qr_login_races.py` | `test_auth_qrlogin_races.py` (with the reset lock order) |
| `test_qr_login_sse.py` | `test_auth_qrlogin_sse.py` |
| `test_geo_source.py` | `test_auth_qrlogin_geo.py` |
| `test_auth_logging.py` extended | `test_auth_qrlogin_logging.py` |
| `test_auth_schema.py` | `test_auth_schema.py` and `test_auth_qrlogin_schema.py` |
| `test_auth_purge.py` extended | `test_auth_purge.py` |
