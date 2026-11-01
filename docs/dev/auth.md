# Auth: accounts, sessions and Google sign-in

How ownvitals signs people in: [ADR 0003](../adr/0003-accounts-and-sessions.md). #10 ports
classbeat's `auth/**` at `d806051`, with ownvitals' names, Google as the only provider and the
changes of ADR 0003 §5 to §8. #11 adds mandatory TOTP, the second-factor gate, session
protection and step-up (ADR 0003 §2 to §4; "Second factor and step-up" below).

Section numbers in docstrings ("ADR 0003 section 5") are **classbeat's** ADR 0003, unless they
say ownvitals. The "Ported unchanged" table of ownvitals' ADR 0003 §1 maps them.

## The pieces

| Path | What |
|---|---|
| `backend/migrations/versions/0005_auth.py` | the twelve account tables (system tables, ADR 0002 §3) and the owner foreign keys of `jobs` and `job_events` to `users` (`ON DELETE CASCADE`) |
| `backend/src/ownvitals/auth/tables.py` | the same tables in SQLAlchemy Core; `test_auth_schema.py` holds the migration to it |
| `backend/src/ownvitals/auth/setup.py` | `install` (routers, the Origin/CSRF middleware, error handlers) and `attach_state`/`detach_state` (keys, hasher, OAuth runtime, mail hooks), called by `ownvitals.web` |
| `backend/src/ownvitals/auth/deps.py` | the only web issuer of a `UserContext` (`require_user` and the others), `request_ip`, and `session_probe` for the jobs event stream |
| `backend/src/ownvitals/auth/owners.py` | `ACCOUNTS`: the jobs issuer's `owner_active` check, for the worker |
| `backend/src/ownvitals/auth/purge.py` | `purge_expired`, which the worker runs every `purge_interval_seconds` |
| `backend/src/ownvitals/auth/operator.py` | what the operator CLI calls (below) |
| `backend/src/ownvitals/flags/` | the sign-up mode only (`invite` or `closed`); #12 adds the stored flags |

## Guard 8 and the public entry points

Only `ownvitals.auth` touches the account tables (`tests/auth_tables_scan.py`,
`tests/test_auth_tables_guard.py`). Code outside the package may import only the names in
`PUBLIC_ENTRY_POINTS`, and only with `from ownvitals.auth.<module> import <name>`:

| Module | Names | Used by |
|---|---|---|
| `setup` | `attach_state`, `detach_state`, `install` | `ownvitals.web` |
| `deps` | `Probe`, `require_user`, `session_probe` | `ownvitals.jobs.deps` |
| `deps` | `require_sensitive`, `require_stepup`, `require_admin_stepup` | the connections layer (#13), the admin writes (#12) |
| `owners` | `ACCOUNTS` | `ownvitals.worker` |
| `purge` | `purge_expired` | `ownvitals.worker.loop` |
| `operator` | `AccountError`, `create_invite`, `grant_admin`, `mfa_reset`, `revoke_admin` | `ownvitals.cli` |

Classbeat re-exported these names lazily from the package (PEP 562). ownvitals cannot: the egress
guard refuses dynamic imports, and an eager package would load the web's sign-in and mail
modules into the worker. A new entry point goes in the guard's list, with a test, in the PR that
needs it.

## Settings

The settings live in [backend.md](backend.md). The ones that matter here:

- `signup_mode`: `invite` (default) or `closed`. `open` is refused until the M8 gate.
- `trusted_proxies`: default loopback only. On the home server, set it to SWAG's address.
- `cf_connecting_ip_proxies`: the trusted proxies whose `CF-Connecting-IP` counts (SWAG). The
  header counts only when the peer is in both lists, it appears exactly once, and it parses to
  an IP that is not itself a trusted proxy. Otherwise the client IP is the right-most
  `X-Forwarded-For` entry that is not a trusted proxy, or the peer.
- `admin_emails`, `session_idle_hours` (72), `session_absolute_days` (30),
  `stepup_window_minutes` (10, never longer), `password_hash_workers`,
  `purge_interval_seconds`, `smtp_total_seconds`.
- `oauth_google_client_id`, and the secret file `oauth_google_client_secret`. Google is on only
  when both exist.
- The secret file `app_secret_key` (at least 32 bytes). Web refuses to start without it; the
  keys of ADR 0003 §1 ("Derived keys") come from it.

## Google sign-in

- Every outbound call goes through `ownvitals.egress`: `google_oauth_client` (Authlib) for the
  authorization URL and the code exchange, and `google_async_client` for the JWKS.
- State, nonce and a 64-character PKCE verifier (S256) are stored per flow in `oauth_flows`.
  The flow is taken by `DELETE … RETURNING` before the code exchange. A callback whose flow has
  no verifier or no nonce is refused, and the table's checks make such a row impossible.
- The ID token is checked by `oauth_idtoken.py` (RS256, `iss`, `aud`/`azp`, `exp`, `iat`,
  `nonce`), with JWKS through the cache. The identity is `(google, sub)`, never the email.
- Re-authentication sends `max_age=0` and asks for `auth_time` through the OIDC `claims`
  parameter. Google adds `auth_time` only when asked, and it must be within 5 minutes.
- An unverified or missing Google email goes to the hand-off (`/signin?oauth=email_needed`):
  the account is created only after a mail link proves an address, in the same browser.
- The frontend button follows Google's branding guidelines (owner, 2026-11-01):
  `frontend/src/components/GoogleLogo.tsx` draws the "G" from the app's bundle, with the
  `--ov-google-*` tokens and their light and dark button variants.

## Second factor and step-up (#11)

- **Full sessions.** A sign-in (password, Google, the first after a sign-up) gives a session
  without its second factor. It becomes **full** when a TOTP or recovery code is verified in it
  (`POST /api/auth/mfa/verify`) or its first enrolment is confirmed; both rotate it. Only a full
  session yields a `UserContext` (`deps.current_context`); `require_user` answers 403
  `two_factor_required` to any other session.
- **The gate** (`auth/gate.py`, run by the middleware after the CSRF check) answers 403
  `two_factor_required` to a session that is not full on every path under `/api` and `/oauth`,
  and every write elsewhere, except `GET /api/auth/session`, `POST /api/auth/logout`,
  `GET /api/health`, `GET /api/ready`, and either the enrolment (`mfa/totp/start`, `confirm`;
  no TOTP yet) or `mfa/verify` (a confirmed TOTP). `test_auth_mandatory_totp.py` sends a request
  to every route the app registers, so a new route can't skip it.
- **The session answer** carries `totp_enabled` and `second_factor` (`enrol`, `verify` or
  `null`); `mfa` means the session is full. The SPA routes from it alone.
- **Ten minutes.** A session that is not full ends ten minutes after its latest proof (the
  sign-in, or the step-up of a TOTP removal); `resolve_session` deletes it and the purge removes
  what is left.
- **Session protection.** Idle 72 hours, absolute 30 days (settings). Rotation on sign-in, the
  second factor, re-authentication, step-up and TOTP removal; a role change, a reset and
  `admin mfa-reset` revoke every session. A sign-in never carries a second factor over from the
  browser's old session.
- **Step-up.** `require_stepup` (a TOTP or recovery code within `stepup_window_minutes`), and
  `require_sensitive` (recent authentication within 10 minutes, then the step-up) for the
  actions that need both; `require_admin_stepup` is `require_admin` with a 5-minute step-up for
  admin writes. Refusals are 403 `step_up_required` and `reauth_required`. The table:

| Action | Dependency |
|---|---|
| Connect, reconnect, disconnect Withings (#13); delete the account; change or set the password; change the email; link or unlink Google | `require_sensitive` |
| Replace or remove (reset) the TOTP; new recovery codes; log out everywhere | `require_stepup` |
| Admin writes (#12) | `require_admin_stepup` |
| First enrolment | the sign-in itself (no factor to step up with) |

- **Audit.** `auth_events` kinds: `mfa_verified`, `step_up`, `reauth_totp`, `mfa_failed` (with
  `detail.purpose`: `verify`, `stepup`, `reauth`, `enroll`), `totp_enrolled`, `totp_replaced`,
  `totp_removed`, `logout_everywhere` (with the number of sessions). Code attempts share the MFA
  rate-limit buckets.
- **Removing the TOTP** (step-up) revokes every other session and rotates this one without its
  second factor, so it goes straight to enrolment.
- **Frontend.** `components/StepUp.tsx`: `useProtected()(purpose, action)` runs an action and,
  on `step_up_required` or `reauth_required`, opens the shared "Confirm it's you" dialog (the
  password first when re-authentication is wanted, "Use Google instead" when Google is linked,
  then the code), then tries again. `pages/SecurityPage.tsx` is the Security tab's "Where you're
  signed in" with "Sign out everywhere…".

## Mail locally

The compose stack runs mailpit (`axllent/mailpit`, pinned by digest). Web sends the account
mails to it over plain SMTP on port 1025, which only the `mailpit` host and loopback are allowed.
The worker gets no SMTP setting. Read the mails at `http://127.0.0.1:${MAILPIT_PORT:-8025}`.

## The operator CLI

These run as the app role, after the database-role check, in the web image:

```bash
python -m ownvitals invite create --expires 7d    # prints one code, once
python -m ownvitals admin grant someone@example.com
python -m ownvitals admin revoke someone@example.com
python -m ownvitals admin mfa-reset someone@example.com   # mails a reset link
```

A refusal (an unknown address, a privileged role) exits 1 with a message that names no
credential.

## TOTP seeds

Seeds use the envelope of ADR 0004 from the first enrolment (`totp_keys.py`). The payload AAD is
`ownvitals-totp-v1|<user>`, and the data key is wrapped under the current KEK with AAD
`ownvitals-totp-dek-v1|<kek id>|<user>`. `rewrap_totp_secrets` moves seeds to the current KEK.

A seed that does not decrypt fails MFA closed and writes `totp_key_unavailable` or
`totp_decrypt_failed`. The table's checks refuse any other form.

The fifth failed code in an hour mails the user once. The count is serialised by the advisory
lock namespace 5, `mfa_failures` (ADR 0005 §9).

## Tests

- **Fakes:** `tests/oidcfake.py` is an in-process Google with its own keys, able to issue
  every bad token. `tests/smtpfake.py` is a local SMTP sink. No test touches the network.
- **Helpers:** `tests/authtools.py`, `mfatools.py`, `oauthtools.py` and `flows.py` hold the
  fixtures (`env`, `menv`, `oenv`, `fake`, `smtp`, ...). `Env.full_login` and
  `flows.signed_in` give a full session (with a synthetic TOTP from `Env.enrol`); `Env.login`
  and `signed_in(..., second_factor=False)` a session that still owes its second factor. A test module imports the fixture
  functions it uses, marked `# noqa: F401 - fixtures`.
- **Suites:** `test_auth_*.py`, `test_oauth_idtoken.py`, `test_totp_envelope.py` and
  `test_flags.py`, run against the throwaway Postgres of [backend.md](backend.md).
- **Purge test:** the worker test of the purge installs the queue (`jobtools.install_queue`),
  because the worker checks the queue before it runs.
