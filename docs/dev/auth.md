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
| `backend/src/ownvitals/auth/tables.py` | the same tables in SQLAlchemy Core (the passkey and QR tables in `tables_passkeys.py`, the `MetaData` and column helpers in `table_base.py`, #249); `test_auth_schema.py` holds the migration to it |
| `backend/src/ownvitals/auth/setup.py` | `install` (routers, the Origin/CSRF middleware, error handlers) and `attach_state`/`detach_state` (keys, hasher, OAuth runtime, mail hooks), called by `ownvitals.web` |
| `backend/src/ownvitals/auth/deps.py` | the only web issuer of a `UserContext` (`require_user` and the others), `request_ip`, and `session_probe` for the jobs event stream |
| `backend/src/ownvitals/auth/userlock.py` | the user-row lock every sign-in, link, reset and revoke takes first ("Lock order" below) |
| `backend/src/ownvitals/auth/owners.py` | `ACCOUNTS`: the jobs issuer's `owner_active` check, for the worker |
| `backend/src/ownvitals/auth/purge.py` | `purge_expired`, which the worker runs every `purge_interval_seconds` |
| `backend/src/ownvitals/auth/disconnect_job.py` | the `withings_disconnect` job kind and its step-up check; `purge` re-exports its names |
| `backend/src/ownvitals/auth/operator.py` | what the operator CLI calls (below) |
| `backend/src/ownvitals/auth/events.py`, `event_origin.py` | `record_event`, the one writer of `auth_events`, and the request's coarse origin it stores ("Security activity" below) |
| `backend/src/ownvitals/auth/activity_routes.py`, `activity_model.py` | `GET /api/auth/activity` and its pure allowlist of kinds and fields (#251) |
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
- `locales` (`OWNVITALS_LOCALES`, default `en,sv`; `config_auth.py`): the languages the operator
  enables. `en` is always on; a code that is not in the manifest is dropped, so the setting can
  switch a supported language off and never add one ("Language" below).
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
- Re-authentication: Google ignores `max_age=0` and the `auth_time` claims request, so the rule
  (password accounts refused, a 300-second flow age, a verified email, no future `auth_time`) is
  in [auth-reauth.md](auth-reauth.md), which is current.
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
  browser's old session. A QR approve-login (ADR 0011 section 5) is no exception: its session is
  full because a code was entered on the approving phone **for that sign-in**, not carried over
  from an earlier session of the desktop browser, and that code never counts for the admin role.
- **Step-up.** `require_stepup` (a TOTP or recovery code within `stepup_window_minutes`), and
  `require_sensitive` (recent authentication within 10 minutes, then the step-up) for the
  actions that need both; `require_admin_stepup` is `require_admin` with a 5-minute step-up for
  admin writes. Refusals are 403 `step_up_required` and `reauth_required`. The table:

| Action | Dependency |
|---|---|
| Connect, reconnect, disconnect Withings (#13); delete the account; change or set the password; change the email; link or unlink Google; add or remove a passkey (ADR 0011) | `require_sensitive` |
| Replace or remove (reset) the TOTP; new recovery codes; log out everywhere | `require_stepup` |
| Admin writes (#12) | `require_admin_stepup` |
| First enrolment | the sign-in itself (no factor to step up with) |

- **Audit.** `auth_events` kinds: `mfa_verified`, `step_up`, `reauth_totp`, `mfa_failed` (with
  `detail.purpose`: `verify`, `stepup`, `reauth`, `enroll`, `qr_approve`), `totp_enrolled`, `totp_replaced`,
  `totp_removed`, `logout_everywhere` (with the number of sessions), `sign_out` and
  `session_signed_out` (#251, "Security activity" below). Code attempts share the MFA
  rate-limit buckets.
- **Removing the TOTP** (step-up) revokes every other session and rotates this one without its
  second factor, so it goes straight to enrolment.
- **Frontend.** `components/StepUp.tsx`: `useProtected()(purpose, action)` runs an action and,
  on `step_up_required` or `reauth_required`, opens the shared "Confirm it's you" dialog (the
  password first when re-authentication is wanted, "Use Google instead" when Google is linked,
  then the code), then tries again. `pages/SecurityPage.tsx` is the Security tab's "Where you're
  signed in" with "Sign out everywhere…".

## Language (#249)

The account's language (ADR 0015 §1 and §2). `users.locale`, `pending_signups.locale` and
`oauth_flows.locale` (migration `0017_account_locale`) are `NULL` or a code of the
`CHECK (locale IN ('en', 'sv'))` allowlist (`tables.LOCALE_CHECK`); the migration that adds a
language widens all three. `NULL` means "follow this browser": existing accounts and those made
by the CLI or an admin (`accounts.create_user` without `locale`). The outbox is not changed (§7).

- **Supported and offered.** `ownvitals/locales.py` reads `frontend/src/i18n/locales.json` as
  `SUPPORTED_LOCALES` (the image carries it at `/app/locales.json`, `Dockerfile`). A language is
  offered when it is supported and enabled (`locales` setting above). The decisions are pure,
  in `ownvitals/locale_policy.py`; `auth/locale.py` binds them to a request.
- **Request locale:** the body's optional `locale` field if offered, else `Accept-Language`
  (only its first 256 bytes and 8 entries; matched by primary subtag, `sv-SE` is `sv`; `q=0` or a
  malformed `q` drops an entry; malformed input falls back and never errors), else `en`.
- **`PUT /api/account/locale`** (`auth/locale_routes.py`): `{"locale": "sv" | "en" | null}`,
  behind `require_user` (a full session; the gate and the middleware's Origin and CSRF checks like
  every other write). Not a step-up action. A value that is not offered, or not a string, is
  `400 locale_not_offered` and is never echoed. It writes only the caller's own row: one
  `UPDATE users ... WHERE id = <ctx.user_id> AND disabled_at IS NULL`, the transaction's only
  statement, so it takes the user row first as the lock order requires.
- **Session:** `GET /api/auth/session` returns `user.locale`, the stored value while it is still
  offered, else `null`.
- **Sign-up** stores the language in effect, through `for_new_account` (`NULL` while only one
  language is offered, so those accounts are not pinned to it): email sign-up through the pending
  row into `users.locale` at the confirmation; Google through the OAuth flow row at `start` (only
  for `sign_in`; `link` and `reauth` flows store `NULL`), then into `users.locale` at the callback
  (`oauth_outcomes._create_account`) or through the hand-off's pending row
  (`oauth_handoff.confirm_provider_signup`). A stored value is re-checked against the offered set
  when the account is created. The flow's state, nonce and PKCE handling are unchanged.
- **Tests:** `tests/test_account_locale.py`.

## Lock order (#248)

One order everywhere, so the pairs below serialise instead of racing or deadlocking:

1. **the `users` row**, `FOR NO KEY UPDATE` (`auth/userlock.py: lock_user_row`; the admin
   helpers' `_locked` takes the same mode without reading `password_hash`; a passkey or QR
   sign-in takes `FOR SHARE` through `passkeys.lock_user`, which conflicts with it);
2. then sessions, identities, passkeys, TOTP and recovery codes, known devices and tokens.

The paths that take it first: password sign-in (`/login`) and Google sign-in
(`oauth_outcomes._sign_in`), **every session rotation** (`sessions.rotate_session`: MFA verify,
TOTP confirm, step-up, password, Google and passkey re-auth, TOTP removal), linking a Google
identity (`oauth_store.add_identity`) and unlinking one, the reset request (`reset_mail`) and
completion (before it consumes the token), the password change, the email change (before it
consumes its token), sign-out-everywhere (`/logout-all`), sign-out (`/logout`) and ending one
session (`DELETE /sessions/{id}`) (#251), TOTP removal (`DELETE /mfa/totp`,
before the TOTP row), `admin mfa-reset` (`accounts.reset_mfa`), `keys wipe` and passkey
registration (`passkeys.lock_user`, same mode) and the admin disable, enable and delete. A
passkey or QR sign-in takes `FOR SHARE` (`passkeys.lock_user(share=True)`); a passkey re-auth
takes `NO KEY UPDATE` at once, because it rotates and two `FOR SHARE` holders that both upgrade
deadlock.

The rules that make it hold:

- **Never check a password under the lock.** The finish re-checks what it proved instead: the
  sign-in compares the locked row's `password_hash` with the one it verified and refuses (401,
  `invalid_credentials`) when a reset or change replaced it or the account was disabled or
  deleted; a link re-checks its session (`userlock.session_alive`) and answers
  `session_mismatch` when a reset or revoke deleted it.
- **Why `NO KEY UPDATE`, not `FOR UPDATE`:** inserting a session, an identity or an
  `auth_events` row takes `FOR KEY SHARE` on `users` (the foreign key). `FOR UPDATE` would block
  it, so a transaction holding a session row and inserting such a row would wait on a reset or
  disable that waits for that session row: a deadlock. `NO KEY UPDATE` conflicts with itself and
  with `FOR SHARE`, which is all the ordering needs.
- **Why rotations lock too:** a reset's `DELETE FROM sessions WHERE user_id = ...` that waits for
  the row a rotation is replacing skips it when the rotation commits and never sees the new row
  (not in its snapshot), so without the lock the rotated session survives the reset.
- **A Google sign-in re-checks its identity** under the lock: a reset that removed it, or a
  disable, committed since it was read, and the sign-in fails (`invalid`).
- **A reset keeps only what its page could show.** Identities linked at or after the reset
  link's `created_at` are removed with the ones the user chose (`remove_methods`). A link stamps
  `created_at` with `clock_timestamp()` under the lock, and `reset_mail` takes the lock before
  it issues the token, so an identity is either committed before the token exists (and listed by
  `reset/info`) or counted as linked after it.

`tests/test_auth_reset_races.py` holds each pair on the real database: sign-in vs reset, link vs
reset, rotation vs reset, Google sign-in vs reset and vs disable, sign-out-everywhere vs sign-in,
admin disable vs sign-in and TOTP removal vs `admin mfa-reset`, each in both orders where both
exist, and the pairs at once with no deadlock or serialization error reaching the user.

## Security activity (#251)

The user's own security events, for the Security tab of #203 and the security page (no new
screen; the frontend words every code through the catalogue).

- **Sign-outs are recorded** (`auth/routes.py`): `POST /logout` and ending this session from the
  list write `sign_out`; ending another of the user's sessions writes `session_signed_out`, which
  the user's remaining sessions see in the list; `/logout-all` writes `logout_everywhere` with
  the number of sessions. No row holds a session id. Each path takes the user row first ("Lock
  order" above): the event's foreign key takes `FOR KEY SHARE` on the user row after the session
  row is locked, which would otherwise deadlock with an account deletion
  (`tests/test_auth_signout_races.py`). The event is written in a savepoint after the revoke: a
  failed insert is logged by class and SQLSTATE and the session still ends. A sign-out that finds
  its session already gone records nothing. No notice mail is defined for a sign-out, so none is
  sent (a new notice kind is a later issue; `auth/notices.py` is unchanged).
- **Origin** (`auth/event_origin.py`, migration `0019_auth_event_origin`): every `auth_events`
  row written during a web request stores the request's browser family and operating system
  (`useragent.BROWSERS`, `SYSTEMS`) and its place (`geo`, the `geo_source` setting; `NULL` is
  "Place unknown"). `EventOriginMiddleware` (installed by `setup.install`, outside
  `AuthMiddleware`) puts the request in a context variable; `record_event` reads it. Never the
  address or the `User-Agent` string. Rows written outside a request (the purge, the CLI) and
  older rows have none. `mfa_failed` now goes through `record_event` as well.
- **`GET /api/auth/activity`** (`auth/activity_routes.py`), behind `require_user` (a full
  session): `{"events": [...], "next_before": str | null}`, newest first. Each event is exactly
  `kind`, `at`, `method` (`password`, `google`, `passkey`, `qr` or null), `browser`, `os` (a
  family, `other`, or null when not recorded) and `place` (`"City, CC"`, `"CC"` or
  `"Place unknown"`). The shown kinds are an allowlist (`activity_model.SHOWN`): sign-ins with
  their method (`sign_in`, `passkey_signin` and `qr_login_completed` are all `sign_in`),
  `sign_in_failed`, `reauth_failed` (a passkey re-auth failure too), `mfa_failed`, the three
  sign-outs, the TOTP and recovery-code changes, `mfa_reset`, the passkey changes
  (`passkey_added` for `passkey_registered`), `password_changed`, `password_reset`,
  `email_changed`, `google_linked` and `google_unlinked`. Every other kind stays hidden until it
  is named there. Nothing else of `detail` is read; `mfa_reset` (the operator's doing) shows no
  origin.
- **Only the caller's rows** (guard 7): `auth_events` is a system table without RLS, so the
  route's `WHERE user_id = <ctx.user_id>` is the wall; no parameter names a user, and a cursor
  only bounds that same filter. `tests/test_auth_activity.py` proves it with two users; the
  other execution contexts never read this table.
- **Paging:** `limit` 1 to 50 (default 20); `before` is the previous page's `next_before`
  (`<microseconds since the epoch>_<event id hex>`, ordered by `(at, id)` so ties never skip or
  repeat a row; index `auth_events_user_at_idx`). A malformed cursor is `422`, echoing nothing.
  Rows are purged after 30 days.
- **Tests:** `test_auth_activity.py`, `test_auth_activity_model.py`,
  `test_auth_signout_races.py`, `test_log_redaction_auth_events.py`.

## Passkeys

Passkeys (ADR 0011, #178) are in [auth-passkeys.md](auth-passkeys.md). A passkey replaces the
password at sign-in, never the TOTP; a passkey re-authentication counts as recent
authentication, never as a step-up. `admin mfa-reset` also removes every passkey, unlinks every
Google identity and clears the password (ADR 0011 §3 amendment, amending ADR 0003 §2): after a
reset, the only way in is the password set through the mailed link. `keys wipe` removes passkeys
and Google identities too and keeps the password.

## QR approve-login

QR approve-login (ADR 0011 section 5, #179) is in [auth-qrlogin.md](auth-qrlogin.md): a signed-in
phone scans a desktop's QR and approves with a TOTP or recovery code entered for that approval;
the desktop gets a new, full session that can never approve another QR login. `admin mfa-reset`
and `keys wipe` also expire every live QR login the account scanned or approved.

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
python -m ownvitals admin mfa-reset someone@example.com   # clears TOTP, passkeys, Google, password; mails a reset link
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
