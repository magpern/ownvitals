# ADR 0004: Provider connections, envelope encryption and the KEK ring

Status: Accepted (2026-10-31, #4). Open questions: see the end.

## Context

ownvitals stores three kinds of secret that belong to a user or to the platform: Withings access
and refresh tokens (M1), TOTP seeds (M0, ADR 0003 §5) and the Withings app's client secret
(M1). PLAN §3 ports classbeat's token vault: envelope encryption with AES-256-GCM, AAD
`user_id|provider`, KEK rotation and rewrap, with provider `withings` and the **webhook-secret
pepper in the KEK ring**. PLAN §4.3 adds **token generations** with compare-and-swap updates and a
crash-safe refresh protocol, because Withings rotates the refresh token on every renewal and keeps
the old one valid only for up to 8 hours or until the new access token is first used
(withings-findings §2).

classbeat ADR 0004 is the base. It was written for a MOWL token (no expiry, no revoke) and later
Spotify tokens. What changes here:

- one provider, Withings, reached over OAuth with a provider-side **revoke**;
- a **token generation** counter raised by every token write, instead of classbeat's
  `wrapped_dek` guard and its later separate `generation` column (§6);
- TOTP seeds are on the envelope scheme from the start (no interim form);
- a **pepper** for webhook-secret HMACs lives in the same ring (§4);
- the Withings tokens give read access to a user's whole health history for up to a year, so a
  leak is a special-category disclosure, not only an account takeover.

#8 builds the envelope and the KEK ring (with the pepper slot), #13 the generic connections layer
with the generation column, rewrap and the keys CLI, and M1 the Withings provider on top.

## Decision

### 1. The `connections` table (#13; Withings columns in M1)

An owned table (ADR 0002 §2), one row per user and provider. Every column is `NOT NULL` unless
marked otherwise.

| Column | Notes |
|---|---|
| `id uuid` | primary key with the `gen_random_uuid()` default, but **the application always supplies it** on insert, before encrypting, because the AAD names it (§2) |
| `owner_id uuid` | `REFERENCES users (id) ON DELETE CASCADE` |
| `provider text` | checked against the provider list (`withings` from M1; #13 admits its test-only provider without making it reachable in production); `UNIQUE (owner_id, provider)` |
| `status text` | `connected`, `needs_reconnect` (#13); M1 adds `blocked_by_provider_plan` (PLAN §4.2) and the state open question 2 decides for a disconnected row |
| `last_error text NULL` | a category from §8, checked against the list; never provider text |
| `public jsonb` | non-secret fields needed without decrypting: for Withings `{app_id, withings_userid, scopes}` (PLAN §5); nothing else personal |
| `app_id`, `withings_userid` | generated from `public` (M1), with `UNIQUE (app_id, withings_userid)`: the one owner-less unique index ADR 0002 §2 allows |
| `secret bytea`, `secret_nonce bytea` | the encrypted payload (§2); nonce 12 bytes |
| `wrapped_dek bytea`, `dek_nonce bytea` | the wrapped data key; 48 and 12 bytes |
| `key_id text` | `^kek-[a-z0-9-]{1,28}$` |
| `token_generation bigint` | default 0; **raised by every write of new tokens** (connect, reconnect, refresh), never by a rewrap or a status change (§6) |
| `refresh_in_flight bigint NULL`, `refresh_intent_at timestamptz NULL` | the durable refresh intent of PLAN §4.3 (M1) |
| `expires_at timestamptz NULL` | the refresh token's expiry when known |
| `connected_at`, `updated_at`, `last_used_at NULL` | timestamps |

- Withings payload (M1): canonical JSON `{"access_token", "refresh_token", "access_expires_at"}`.
  The token response's `userid` and `scope` go to `public`.
- `UserContext.connections` holds the user's `(provider, id)` pairs, never secrets or status.
  The web issuer reads them in two steps, as classbeat: a context without connections first, then
  the pairs through `ownvitals.connections` in that user's session, then the final context.

### 2. Envelope encryption (#8)

- **One pure module**, `ownvitals.crypto.envelope` (no database access), does every encryption,
  decryption, wrap and unwrap. Only an allowlist of modules imports it or `cryptography`'s
  primitives: `ownvitals.connections`, `ownvitals.auth` (TOTP, and its HKDF keys),
  `ownvitals.crypto.keks`, and from M1 the module holding the Withings app secret. The crypto guard
  fails on any other (#8 `test_crypto_guard.py`).
- **Encodings:** UUIDs in canonical text form; canonical JSON
  (`sort_keys=True, separators=(",", ":"), ensure_ascii=False`, UTF-8). AAD fields are joined with
  `|`, and no field can contain one.
- **Payload:** AES-256-GCM under a **data key (DEK)** of 32 random bytes, **new for every write**,
  with a random 96-bit nonce; a DEK encrypts exactly one payload, so a nonce can't repeat under a
  key. **Wrap:** the DEK under the KEK named by `key_id`, AES-256-GCM, random nonce.
- **AAD**, versioned per kind of value:

| Value | Payload AAD | Wrap AAD |
|---|---|---|
| Connection tokens | `ownvitals-conn-v1\|<owner_id>\|<provider>\|<id>` | `ownvitals-dek-v1\|<key_id>\|<owner_id>\|<provider>\|<id>` |
| TOTP seed (ADR 0003 §5) | `ownvitals-totp-v1\|<user_id>` | `ownvitals-totp-dek-v1\|<key_id>\|<user_id>` |
| Withings app client secret (M1, `withings_apps`) | `ownvitals-app-v1\|<app_id>` | `ownvitals-app-dek-v1\|<key_id>\|<app_id>` |

  The connection AAD contains PLAN §3's `user_id|provider` binding; the version prefix separates
  the kinds, and the row id stops ciphertext moving between rows of the same user and provider.
  Ciphertext copied into another user's row, provider or row fails to decrypt (#8 wrong-AAD
  tests).

### 3. The KEK ring, rotation and compromise (#8, #13)

- **KEK files:** `connection_kek` (current; its id from a setting, default `kek-1`, checked
  against the `key_id` pattern) and `connection_kek_<key_id>` (retired, decrypt only). A file
  holds exactly 64 lowercase hex characters (32 bytes), optionally one newline. A row's validated
  `key_id` names the file to open; no other input builds a file name. Every KEK file is a
  read-only secret on web and worker. **Web and worker refuse to start** without a valid current
  KEK (#8).
- **Check value:** a system table `connection_keks (key_id primary key, check_value, created_at)`
  stores the first 16 bytes of `HMAC-SHA256(KEK, "ownvitals-kek-check-v1")`. On start, web and
  worker insert the current KEK's row if absent, read it back and compare, and refuse on a
  mismatch (a swapped key under an unchanged id). A retired file without a row, or with another
  value, isn't loaded and counts as `key_unavailable`.
- **Decrypt failures:** a missing or rejected KEK is `key_unavailable` (an operator error: the
  request fails, the row is not changed). A GCM failure is `decrypt_failed`: the status becomes
  `needs_reconnect` by a conditional update (§6). Only the category and row id are logged; no
  other key is tried.
- **Rotation (routine):** install the new KEK as `connection_kek` with a new id and keep the old
  one as `connection_kek_<old id>`, restart, run `python -m ownvitals keys rewrap`; once it reports
  zero rows for the old id in connections, TOTP seeds and (M1) the app secret, remove the old file
  from the stack. Keep one copy with the backups until every backup taken before the rotation has
  aged out, then destroy it.
- **Rewrap mechanics** (#13 `test_keys_rewrap.py`): connections per owner as `ownvitals_app`
  with `app.user_id` set (no RLS bypass); TOTP seeds through `ownvitals.auth` (guard 8). Per row,
  its own transaction: `SELECT … FOR UPDATE`, unwrap,
  wrap under the current KEK, `UPDATE … WHERE id = :id AND key_id = :old`. It changes only the
  wrap, never the payload or `token_generation`, prints counts per `key_id` only, and is
  resumable.
- **After a suspected KEK compromise, everything goes** (classbeat ADR 0004 §2): rewrap does not
  re-encrypt payloads, so an old KEK plus an old dump still reads a rewrapped row. The response:
  1. stop web and worker;
  2. install a new KEK under a new id and remove every old KEK file; if the secrets directory
     leaked, also rotate `app_secret_key`, the database passwords and the pepper (§4);
  3. `docker compose --profile maint run --rm maint python -m ownvitals keys wipe --confirm`, as
     the owner role (never given to web or worker), with no KEK. It refuses while any app-role
     session is connected or while the configured KEK id already has a check row. Per user, in one
     transaction: an `auth_events` row `connections_wiped`, the user's connection rows deleted,
     and TOTP reset the way `admin mfa-reset` does; after the commit, a mail saying what was
     removed and what to do, with no token or account detail. Then every check row is deleted,
     the affected tables vacuumed (`VACUUM (FULL)`) and a checkpoint taken (#13
     `test_keys_wipe.py`);
  4. backups taken before the wipe, WAL archives included, count as compromised: destroy them,
     or wipe again after restoring one;
  5. start web and worker; users reconnect and re-enrol.
- **What is different for Withings** (M1; open question 3): the leaked tokens stay valid at
  Withings until revoked, and Withings, unlike MOWL, can revoke; the leaked KEK also exposed the
  app's client secret, which the owner rotates at Withings and re-enters; and the pepper, which
  usually sits in the same secrets directory, is rotated with every webhook secret (§4). The
  ported wipe deletes connection rows; once health rows reference `connections` (M1), deleting a
  row would cascade to, or be blocked by, the user's synced health data, so M1 makes the wipe
  clear the secret columns instead, consistent with whatever open question 2 decides for
  disconnect. Health data itself is not under the KEK: a KEK leak alone doesn't expose it, though
  the host compromise that leaks a KEK usually does (threat model T60).

### 4. The webhook-secret pepper (#8 slot; used from M1)

- PLAN §4.4: each Withings app has 256-bit webhook secrets, stored only as
  **`HMAC-SHA256(pepper, secret)`** with a `secret_id`, compared in constant time; during a
  rotation two are active.
- **The pepper is a key in the KEK ring**, of its own kind: a secret file `webhook_pepper`
  (current; id from a setting, default `pep-1`, pattern `^pep-[a-z0-9-]{1,28}$`) and
  `webhook_pepper_<id>` (retired), in the KEK file format, with its own check row
  (`HMAC-SHA256(pepper, "ownvitals-pepper-check-v1")`) in `connection_keks`. A KEK can't be loaded
  as a pepper or the other way round. It never encrypts anything.
- **M0 (#8):** the ring defines the slot and validates the file if present; nothing uses it.
  **M1:** web (the receiver) and worker (rotation) require it.
- Each stored entry records the pepper id it was made with: `webhook_secrets` entries are
  `{secret_id, pepper_id, hmac, active_from, retire_at}`, PLAN §5's fields plus `pepper_id`.
- **Rotating the pepper is a webhook-secret rotation** (PLAN §4.4): a new secret under the new
  pepper, `notify update` on every subscription, then the old secret and pepper are retired.
- **Why the ring and not a key derived from `app_secret_key`:** rotating `app_secret_key` (CSRF,
  devices, recovery codes) must not break webhook delivery for every user; and the pepper, like the
  KEKs, belongs outside the database and its dumps, backed up separately.
- The receiver (M1) compares `HMAC(pepper of the entry's pepper_id, presented secret)` with each
  active entry by `hmac.compare_digest`. M1 should reject an unknown secret before any database
  work, for example from an in-process copy of the active entries (threat model T38).

### 5. From the row to the provider call (#13; M1)

- `ownvitals.connections` is the only module that turns rows into credentials. Its repository
  takes a `UserContext` and returns a credentials object with a redacting `__repr__`/`__str__`,
  never serialized, never a dict of plaintext fields.
- Jobs store only the connection id; the worker rebuilds the credentials from the job's
  `UserContext` (ADR 0005 §4) when it runs and drops them when the run ends. Credentials never go
  into job rows, events, SSE, logs, error messages or model input. A job whose connection is gone
  or not `connected` stops before any provider call with `not_connected`.
- Withings calls are async httpx through the egress module (#6), so classbeat's worker-thread
  wrapper for a synchronous library is not needed.

### 6. Token generations and the Withings refresh (#13 seam; M1)

- **#13** adds `token_generation` and one helper for every token write:
  `UPDATE connections SET … , token_generation = :g + 1 WHERE id = :id AND token_generation = :g`;
  0 rows means a newer generation exists, and the caller gives up (#13 CAS test). An older
  generation can never overwrite a newer one (PLAN §4.3).
- **Status changes** (`needs_reconnect`, `last_error`, `last_used_at`) are conditional on the
  generation that was read, so a stale 401 from before a refresh or reconnect can't flip a fresh
  connection. One counter does what classbeat needed `wrapped_dek` and, later, a separate
  `generation` column for.
- **The M1 refresh protocol is PLAN §4.3** (intent row, call, one CAS transaction to
  `generation + 1`, first use only after the commit, one recovery retry within 8 hours, then
  `needs_reconnect`; never the generic retry path). This ADR adds the constraints M1 must meet:
  - the per-connection lock covers steps 1 to 3. A transaction-scoped lock means the Withings
    call's total deadline stays below `ownvitals_app`'s 60 s idle-in-transaction and 30 s statement
    timeouts (ADR 0002 §5); a session-scoped advisory lock lives on a dedicated connection and is
    released explicitly (the pool's `DISCARD ALL` also releases it);
  - inside a job, the intent and the CAS write are fenced writes (ADR 0005 §4), so a worker that
    lost its lease can't record a refresh;
  - `TokenInvalid` (401, 2553, 2555; PLAN §4.2) goes to this path, never to a generic retry;
  - PLAN §4.3's crash and race tests are M1's acceptance.

### 7. Connect, reconnect and disconnect (#13 routes; M1 Withings)

- The routes need **recent authentication and step-up** (ADR 0003 §4) and CSRF. Until M1,
  connect answers `unknown_provider`; #13's test-only provider exercises the flow (#13 test:
  refused without step-up).
- **Withings connect (M1, PLAN §4.3):** `start` stores the OAuth `state` hashed, bound to the
  session, 10-minute TTL, single use; the callback takes it atomically before anything else, then
  **exchanges the code within its 30-second validity in the request itself** (open question 1),
  upserts by `(app_id, withings_userid)` and refuses a Withings account linked to another
  ownvitals account. The write follows classbeat's path: the provider call first, outside any
  transaction; then one transaction that locks `(owner_id, provider)` `FOR UPDATE`, keeps or
  supplies the row id, encrypts under a new DEK, and updates or inserts with
  `token_generation + 1`. Never `INSERT … ON CONFLICT DO UPDATE`, which would keep an old id with a
  payload encrypted for a new one.
- **Rate limits:** connect start per IP (classbeat's OAuth start limit, 30 per 15 minutes) and per
  user. No Withings password ever passes through ownvitals, so classbeat's per-MOWL-email limit has
  no counterpart.
- **Disconnect (PLAN §4.3):** `notify revoke` for each `appli`, then `oauth2 revoke`, then the
  secrets are deleted. Account deletion does the same before the purge. What happens to the
  connection row, to the health data already synced, and when a revoke fails is open
  question 2. Once a revoke has succeeded, the wrapped tokens left in older backups are useless at
  Withings (unlike classbeat's MOWL tokens).

### 8. Errors and status

| Category | From (PLAN §4.2) |
|---|---|
| `needs_reconnect` | `TokenInvalid` after the §6 path gives up |
| `rate_limited` | status 601: the app bucket holds (ADR 0005 §8) |
| `upstream_unavailable` | HTTP 408, 429, 5xx, timeouts, connection errors |
| `permanent` categories | other non-zero `status` values, mapped by number to a fixed list in M1 |
| `blocked_by_provider_plan` | the post-13-Oct-2026 Withings+ case, once M1/M2 learn its signal (PLAN §4.2) |
| `key_unavailable`, `decrypt_failed` | §3 |
| `not_connected` | §5 |
| `unknown` | anything else |

- Withings answers errors as HTTP 200 with a non-zero `status` (withings-findings §2); the client
  checks it on every response.
- Categories come from types, HTTP status and the numeric `status`, **never from message text**,
  and provider text is never stored (PLAN §4.2). As in classbeat ADR 0004 §5, the wrapper records
  what it needs inside `except`, raises its own `ConnectionFailure(category)` after the block (no
  `__cause__`, no `__context__`, `args == (category,)`), the `except` block can't raise, and no
  error reporter or log option captures frame locals.

### 9. Logging and redaction

- The log formatter masks keys named `token`, `access_token`, `refresh_token`, `code`, `state`,
  `client_secret`, `secret`, `password`, `authorization` and `cookie`, and anything under a
  connection row's secret columns. **Withings user ids are not logged**; the connection id is.
  Webhook paths are redacted (PLAN §4.4).
- httpx and provider loggers run at `WARNING`; headers and bodies are never logged.
- Tests (#13; M1 extends them to Withings): caplog over connect, use and every error path shows
  no token, code or state; a `pg_dump` of a test database with a planted token doesn't contain it;
  `repr` of credentials contains no secret; a `ConnectionFailure` has neither cause nor context;
  the e2e `docker inspect` check (#14) covers `connection_kek`, and `webhook_pepper` from M1.

## Alternatives

- **`pgcrypto`:** the key would travel to the database in every query and could reach its logs.
- **One key for every row, no envelope:** rotation would re-encrypt every payload, and one
  leaked value would expose the column.
- **KEK or pepper derived from `app_secret_key`:** couples unrelated rotations (§4).
- **A KMS or Vault:** not available on the home server; the KEK loader is the seam where one plugs
  in later (VPS, M8).
- **Webhook secrets stored encrypted rather than as HMACs:** verification needs no plaintext; a
  peppered HMAC can't be turned back into a secret from a dump.
- **Envelope-encrypting the health data columns:** analysis runs in SQL over the values, and the
  KEK sits on the same host; it would not change who can read them. The threat model records the
  plaintext-at-rest risk instead.

## Consequences

- #8: the envelope module, the KEK ring with its check table and the pepper slot, the crypto
  guard. #13: the `connections` table with `token_generation` and its CAS helper, step-up on
  connect and disconnect, `keys rewrap` and `keys wipe`. M1: the Withings provider, the refresh
  protocol, the pepper in use, the app secret.
- Losing the current KEK means every user reconnects Withings and re-enrols TOTP; there is no
  recovery path by design.
- Backups hold tokens and seeds as ciphertext only; restoring one needs the KEK files of its time,
  backed up separately (§9.2).
- Threat model T29 to T43 cite this ADR.

## Open questions

1. **Who calls the Withings token endpoint during the callback (Architect; M1).** PLAN §2 says the
   worker owns every outbound call; PLAN §4.3 and withings-findings §2 say the code must be
   exchanged in the callback request, within 30 seconds, not through a queued job. The callback
   runs in the web process. Proposed: the web process gets one narrow egress entry, used only for
   the authorization-code exchange (`requesttoken` with `grant_type=authorization_code`) on
   `wbsapi.withings.net`, like its Google sign-in hosts (#6), and that call claims a rate-limit
   token like any other. Every other Withings call stays in the worker. If that reading of PLAN §2
   is not acceptable, this needs a roadmap amendment.
2. **Reconnect to another Withings account, and disconnect semantics (owner for data retention;
   Architect for the rest; M1).** `UNIQUE (owner_id, provider)` allows one Withings connection per
   user, while PLAN §4.3 upserts by `(app_id, withings_userid)`. A connected user completing OAuth
   as a different Withings user can be refused ("disconnect first", the safest default) or have the
   connection replaced, and then the health data from the first account must not be mixed with the
   second's. PLAN §4.3's disconnect "deletes secrets": it doesn't say whether the row stays (a
   `disconnected` status) or whether the synced health data is kept, deleted, or kept until the
   user deletes it, which is a product and data-protection choice. Nor does it say what happens
   when a revoke fails: keeping the secrets keeps upstream access alive; deleting them may make a
   later revoke impossible (`notify revoke` needs the user's access token). The same choice
   decides what `keys wipe` does to a connection row (§3).
3. **Upstream revoke after a KEK compromise (Architect; M1).** The ported `keys wipe` runs without
   a KEK or outbound access, so it can't revoke at Withings. Either the wipe gains a revoke step
   (the worker, with the old KEK, revokes every connection before the wipe deletes it), or the
   runbook relies on users and the owner revoking at Withings. M1 decides and tests it.
