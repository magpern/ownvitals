# ADR 0004: Provider connections, envelope encryption and the KEK ring

Status: Accepted (2026-10-31, #4). Its three open questions were decided by the Architect on
2026-10-31 (§7 the code exchange, recorded as [roadmap Amendment 1](../roadmap.md); §7 reconnect
and disconnect; §3 revocation after a KEK compromise), and so was the source of the webhook
secrets (§4), raised in review.

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
- a **pepper** for webhook-secret HMACs and a **seed** the webhook secrets are derived from live
  in the same ring (§4);
- the Withings tokens give read access to a user's whole health history for up to a year, so a
  leak is a special-category disclosure, not only an account takeover.

#8 builds the envelope and the KEK ring (with the pepper and seed slots), #13 the generic
connections layer with the generation column, rewrap and the keys CLI, and M1 the Withings
provider on top.

## Decision

### 1. The `connections` table (#13; Withings columns in M1)

An owned table (ADR 0002 §2): at most one live connection per user and provider, and rows of
earlier, disconnected Withings accounts kept with their imported data (§7). Every column is
`NOT NULL` unless marked otherwise.

| Column | Notes |
|---|---|
| `id uuid` | primary key with the `gen_random_uuid()` default, but **the application always supplies it** on insert, before encrypting, because the AAD names it (§2) |
| `owner_id uuid` | `REFERENCES users (id) ON DELETE CASCADE` |
| `provider text` | checked against the provider list (`withings` from M1; #13 admits its test-only provider without making it reachable in production). #13 creates a unique index on `(owner_id, provider) WHERE status <> 'disconnected'` (one live connection; §7), not a plain `UNIQUE (owner_id, provider)`, so a `disconnected` row never blocks a new connection |
| `status text` | `connected`, `needs_reconnect` (#13); M1 adds `blocked_by_provider_plan` (PLAN §4.2) and `disconnected` (§7, #27) |
| `last_error text NULL` | a category from §8, checked against the list; never provider text |
| `public jsonb` | non-secret fields needed without decrypting: for Withings `{app_id, withings_userid, scopes}` (PLAN §5); nothing else personal |
| `app_id`, `withings_userid` | generated from `public` (M1), with `UNIQUE (app_id, withings_userid)`: the one owner-less unique index ADR 0002 §2 allows |
| `secret bytea NULL`, `secret_nonce bytea NULL` | the encrypted payload (§2); nonce 12 bytes |
| `wrapped_dek bytea NULL`, `dek_nonce bytea NULL` | the wrapped data key; 48 and 12 bytes |
| `key_id text NULL` | `^kek-[a-z0-9-]{1,28}$` |
| (check) | the five credential columns are all set or all `NULL`; set when `connected`, `NULL` when `disconnected`, either when `needs_reconnect` (a 401 keeps them, `keys wipe` removes them, §3) |
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
  2. install a new KEK under a new id and remove every old KEK file. A compromise of the KEK
     ring counts as a seed compromise too, so install a new webhook seed and pepper the same way
     (§4); if the secrets directory leaked, also rotate `app_secret_key` and the database
     passwords;
  3. `docker compose --profile maint run --rm maint python -m ownvitals keys wipe --confirm`, as
     the owner role (never given to web or worker), with no KEK. It refuses while any app-role
     session is connected or while the configured KEK id already has a check row. Per user, in one
     transaction: an `auth_events` row `connections_wiped`; **each connection row set to
     `needs_reconnect` with its credential columns removed** (classbeat deletes the rows;
     ownvitals keeps them, so the imported health data that references them stays, Architect
     decision 2026-10-31); and TOTP reset the way `admin mfa-reset` does. After the commit, a mail
     says what was removed and what to do, with no token or account detail. Then every check row
     is deleted, the affected tables vacuumed (`VACUUM (FULL)`) and a checkpoint taken (#13
     `test_keys_wipe.py`);
  4. **rotate the Withings app's client secret** in the Withings developer dashboard and enter
     the new one (M1 runbook step, owner): a stolen refresh token can't be used without it;
  5. backups taken before the wipe, WAL archives included, count as compromised: destroy them,
     or wipe again after restoring one;
  6. start web and worker; run #22's `rotate`: the worker adds a webhook secret derived from the
     new seed and **retires every old webhook secret at once** (§4); users reconnect and
     re-enrol.
- **Revocation after a compromise is not attempted** (Architect decision, 2026-10-31). The wipe
  has no KEK and no outbound access, so it can't revoke at Withings. Step 4 makes every stolen
  refresh token useless; a stolen access token stays valid for up to its 3-hour lifetime
  (withings-findings §2). That window is an accepted risk (threat model, accepted risk 19).
- **Webhook subscriptions after a seed compromise (or a compromise of the KEK ring that holds
  the seed)** (Architect decision, 2026-10-31): the seed and the pepper are rotated and the old
  secret retired at once. Re-pointing a subscription (`notify update`) needs the user's access
  token: where the connection still has one, the worker re-points it; where the wipe removed it,
  the subscription keeps the old callback URL until the user reconnects, and reconnect
  subscribes with the new secret (§4). Until then, notifications to the old URL get the generic
  200 and are ignored. Payloads are only hints, and after reconnect the backfill, poll and
  reconciliation bring the data up to date (D3). Accepted risk 20 of the threat model.
- Health data itself is not under the KEK: a KEK leak alone doesn't expose it, though the host
  compromise that leaks a KEK usually does (threat model T60).

### 4. Webhook secrets: the seed and the pepper (#8 slots; used from M1)

- PLAN §4.4: each Withings app has 256-bit webhook secrets in its callback URL
  (`/api/webhooks/withings/{secret}`), stored only as **`HMAC-SHA256(pepper, secret)`** with a
  `secret_id`, compared in constant time; during a rotation two are active. Withings subscribes
  per user and `appli` with that URL, so the worker needs the plaintext again whenever it
  subscribes (connect, #24), repairs drift (#26) or re-points a subscription (`notify update`).
- **The secret is derived, never stored** (Architect decision, 2026-10-31):
  `secret = HMAC-SHA256(seed[seed_id], "ownvitals-webhook-v1" ‖ app_id ‖ secret_id)`, where ‖ is
  the `|` join of §2's encodings; 32 bytes, encoded base64url without padding. Each
  `webhook_secrets` entry records `{secret_id, seed_id, pepper_id, hmac, active_from, retire_at}`:
  PLAN §5's fields plus the ids of the seed it was derived from and of the pepper its HMAC was
  made with. **A seed rotation is a secret rotation**: a new `secret_id` under the new seed. A
  dump yields no secret and no way to compute one.
- **Two keys in the KEK ring**, each of its own kind, in the KEK file format, with its own check
  row in `connection_keks`; a key of one kind can't be loaded as another:
  - the **seed**: a secret file `webhook_seed` (current; id from a setting, default `seed-1`,
    pattern `^seed-[a-z0-9-]{1,28}$`) and `webhook_seed_<id>` (retired), check value
    `HMAC-SHA256(seed, "ownvitals-seed-check-v1")`. **Only the worker** mounts and loads it: it
    derives a secret to build a callback URL and to add or re-hash a `webhook_secrets` entry;
  - the **pepper**: `webhook_pepper` (current; default `pep-1`, pattern `^pep-[a-z0-9-]{1,28}$`)
    and `webhook_pepper_<id>` (retired), check value
    `HMAC-SHA256(pepper, "ownvitals-pepper-check-v1")`. Web loads it to verify; the worker loads
    it to compute and check entries' HMACs. It never encrypts anything.

  **Web never holds the seed**: its service never mounts it, and its ring loader refuses the
  seed kind (#8 test). That is the property that matters. The worker holding the pepper as well
  adds nothing, since a holder of the seed can derive every secret anyway (Architect decision,
  2026-10-31).
- **Verification (web, #24):** for each active entry, `HMAC(pepper[pepper_id], presented
  secret)` compared with the entry's `hmac` by `hmac.compare_digest`: one constant-time
  comparison per active secret, with the pepper the entry names. An entry whose pepper isn't
  loaded never matches. M1 should reject an unknown secret before any database work, for example
  from an in-process copy of the active entries (threat model T38).
- **Derivation check (worker):** derive with the entry's `seed_id`, then check that
  `HMAC(pepper[pepper_id], derived)` equals the entry's `hmac`; a missing key or a mismatch
  refuses and builds no URL (fail closed). The plaintext lives only in memory for the call; it
  is never logged, stored or put in a job row, and the callback URL is redacted like the webhook
  path (§9).
- **Who writes the entries** (Architect decision, 2026-10-31). Rotation needs the seed and
  `notify update`, both worker-only, so the `webhook_secrets` entries are not written by the
  operator tool that writes the rest of `withings_apps` (ADR 0002 §3). #22's `rotate` command
  only asks the worker to rotate (#18's system work: the app-level steps run as owner-less
  upkeep, since no queue row is without an owner, ADR 0005 §8; each `notify update` runs as an
  owned job of the connection's owner). The worker derives the new secret and calls **one
  reviewed `SECURITY DEFINER` function owned by `ownvitals_owner`**, which can only append,
  retire or re-hash entries in the `webhook_secrets` column of one `withings_apps` row, writes
  the admin-audit row, and touches no other column. It is pinned like the other
  definer functions (ADR 0005 §3), and a guard makes the worker's rotation module its only
  caller. Web shares the app role, so code running in web could still call it: accepted risk 21
  of the threat model.
- **Secret rotation** (PLAN §4.4): the worker appends an entry for a new `secret_id` under the
  current seed and pepper, re-points every subscription with `notify update` (per user, with
  that user's token), then retires the old entry (`retire_at`). A seed rotation is such a
  rotation; the old seed file is removed once no active entry names its `seed_id`.
- **Pepper rotation** needs no new secret and no `notify update`: install the new pepper as
  current; the worker re-hashes every entry under it through the same function (it derives each
  secret from its seed) and sets its `pepper_id`, one entry at a time. The old pepper is
  retired, and its file removed, only when no entry references its id, so the rotation is
  checkable with one query.
- **After a seed compromise (or a compromise of the KEK ring that holds the seed)** the seed and
  the pepper are rotated and the old secret is retired at once. Subscriptions are re-pointed
  with `notify update` where the connection still has a token, as in any rotation, and on
  reconnect otherwise (§3; Architect decision, 2026-10-31).
- **M0 (#8):** the ring defines both slots and validates the files if present; nothing uses them.
  **M1:** web requires the pepper, the worker the seed and the pepper.
- **Why the ring and not keys derived from `app_secret_key`:** rotating `app_secret_key` (CSRF,
  devices, recovery codes) must not break webhook delivery for every user; and the seed and the
  pepper, like the KEKs, belong outside the database and its dumps, backed up separately.

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
  - the per-connection lock covers steps 1 to 3, and step 1 commits the intent, so it is a
    **session-scoped advisory lock** (`pg_advisory_lock`), not a transaction-scoped one. It is
    taken on a dedicated connection that holds no transaction while the Withings call runs (so
    the 60 s idle-in-transaction timeout of ADR 0002 §5 doesn't apply), and released explicitly
    after step 3; if the worker dies, the connection's end releases it, and the pool's
    `DISCARD ALL` releases it on reuse. The intent commit and the CAS write are short
    transactions of their own;
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
  **exchanges the code within its 30-second validity in the request itself**.
- **The code exchange is the web process's only Withings call** (roadmap Amendment 1, Architect
  decision 2026-10-31, resolving PLAN §2 "the worker owns every outbound call" against §4.3). The
  web process gets one narrow web-only egress entry, used solely for the `oauth2 requesttoken`
  authorization-code exchange on `wbsapi.withings.net` inside the callback; it counts against the
  app's rate budget like any other call. Every other Withings call, refresh and revoke included,
  stays in the worker.
- **Which row the tokens go to** (Architect decision, 2026-10-31), by `(app_id, withings_userid)`:
  - a row of **another ownvitals account** holds that Withings account: refused (PLAN §4.3);
  - the user has a **live connection to a different Withings account**: refused until the user
    disconnects it;
  - the user's own row for this Withings account, live or `disconnected`: that row is reused
    (`connected` again, the new tokens, `token_generation + 1`), and its imported data with it.
    The same write clears `refresh_in_flight` and `refresh_intent_at`, so a stale intent from
    the old grant can't start `token_refresh_recover` against the new tokens;
  - otherwise a new row.

  The write follows classbeat's path: the provider call first, outside any transaction; then one
  transaction that locks the user's rows for the provider `FOR UPDATE`, keeps or supplies the row
  id, encrypts under a new DEK, and updates or inserts. Never `INSERT … ON CONFLICT DO UPDATE`,
  which would keep an old id with a payload encrypted for a new one.
- The `UNIQUE (app_id, withings_userid)` index covers `disconnected` rows too, so a Withings
  account stays tied to the ownvitals account that holds its imported data; another account can
  link it only once that row is gone: account deletion, or a disconnect with "also delete
  imported data", which removes the row too (#27).
- **Rate limits:** connect start per IP (classbeat's OAuth start limit, 30 per 15 minutes) and per
  user. No Withings password ever passes through ownvitals, so classbeat's per-MOWL-email limit has
  no counterpart.
- **Disconnect** (PLAN §4.3; Architect decision, 2026-10-31; #27):
  1. upstream revoke in the worker: `notify revoke` for each `appli`, then `oauth2 revoke`;
  2. the secrets are deleted: the credential columns become `NULL`;
  3. the row is kept with status `disconnected`, without credentials. Webhooks for it are
     answered like an unknown user's and start no job.
  - **Imported data is kept by default**, with an explicit "also delete imported data" option.
    Deleting is irreversible and keeping is reversible, so keeping is the default; the option is
    part of the disconnect request and so covered by its step-up.
  - **If an upstream revoke fails, the secrets are deleted anyway**; the failure is recorded on
    the row (a category, §8) and the revoke retried within a budget. `notify revoke` takes the
    user's access token, so a retry that would need a deleted token can't run: #27 records that
    case as not revoked and tells the user how to remove the app's access at Withings.
  - Account deletion runs steps 1 and 2 before the purge, which then deletes every row.
  - Once a revoke has succeeded, the wrapped tokens left in older backups are useless at
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
| `revoke_failed` | an upstream revoke at disconnect that failed (§7); recorded on the `disconnected` row |
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
  the e2e `docker inspect` check (#14) covers `connection_kek`, and `webhook_pepper` and
  `webhook_seed` from M1.

## Alternatives

- **`pgcrypto`:** the key would travel to the database in every query and could reach its logs.
- **One key for every row, no envelope:** rotation would re-encrypt every payload, and one
  leaked value would expose the column.
- **KEK, seed or pepper derived from `app_secret_key`:** couples unrelated rotations (§4).
- **A KMS or Vault:** not available on the home server; the KEK loader is the seam where one plugs
  in later (VPS, M8).
- **Webhook secrets stored encrypted under the KEK, next to their HMAC:** it would give the worker
  its plaintext too, but puts a recoverable secret in the database, against PLAN §4.4's "stored
  only as HMAC". Deriving it from an off-database seed keeps the database at HMACs only.
- **Webhook secrets generated randomly and never kept:** nothing could rebuild the callback URL
  for a later subscribe, drift repair or `notify update`.
- **Envelope-encrypting the health data columns:** analysis runs in SQL over the values, and the
  KEK sits on the same host; it would not change who can read them. The threat model records the
  plaintext-at-rest risk instead.

## Consequences

- #8: the envelope module, the KEK ring with its check table and the pepper and seed slots, the
  crypto guard. #13: the `connections` table with `token_generation` and its CAS helper, nullable
  credential columns with their check, the partial unique index on `(owner_id, provider)`,
  step-up on connect and disconnect, `keys rewrap` and `keys wipe` (rows to `needs_reconnect`,
  credentials removed). M1: #22 the web egress entry of roadmap Amendment 1, the refresh
  protocol, the app secret and the `withings_apps` CLI (owner role); #24 the derived webhook
  secrets with the seed and the pepper in use, the rotation and its definer function; #27 disconnect, the `disconnected` status, the "also delete
  imported data" option, revoke retries.
- Losing the current KEK means every user reconnects Withings and re-enrols TOTP; there is no
  recovery path by design.
- Backups hold tokens and seeds as ciphertext only; restoring one needs the KEK files of its time,
  backed up separately (§9.2).
- Threat model T29 to T43 cite this ADR.
