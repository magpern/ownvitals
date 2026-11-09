# Connections, rewrap and wipe

ADR 0004 sections 1, 3 and 5 to 9. The table is migration `0007_connections`. Credentials are
envelope ciphertext (ADR 0004 section 2, [crypto.md](crypto.md)): payload AAD
`ownvitals-conn-v1|<owner_id>|<provider>|<id>`, wrap AAD
`ownvitals-dek-v1|<key_id>|<owner_id>|<provider>|<id>`. The application supplies the row id
before it encrypts. Nothing here stores or logs a token.

## The row

`connections` is an owned table (forced RLS, `UNIQUE (owner_id, id)`). One live row per user
and provider: unique index `connections_one_live` on `(owner_id, provider)` where
`status <> 'disconnected'`. A disconnected row stays, so a later connection is a new row and
does not reuse a ciphertext encrypted for the old id.

The five credential columns (`secret`, `secret_nonce`, `wrapped_dek`, `dek_nonce`, `key_id`)
are all set or all null. `connected` requires them, `disconnected` forbids them,
`needs_reconnect` allows either. `token_generation` starts at 0 and increases by one on every
token write (`write_tokens`: `UPDATE ... WHERE token_generation = :g`, zero rows means a newer
generation won and the write changes nothing). A rewrap, a disconnect and a wipe do not change
it. `refresh_in_flight` and `refresh_intent_at` are the M1 refresh seam; a token write clears
them.

The provider check admits `withings` (M1, not connected yet) and `test`. Routes offer `test`
only when `environment` is `test`. Any other provider answers
`404 {"detail": {"reason": "unknown_provider"}}`, except `DELETE` of `withings` (below).

## Routes

`GET /api/connections` (a full session) lists id, provider, status, error category and dates.
No ciphertext, no `public`, no tokens.

`POST /api/connections/{provider}` and `DELETE /api/connections/{provider}` use
`require_sensitive`: recent authentication, then a step-up (ADR 0003 section 4). The test
provider's body is `access_token` and `refresh_token` (8 to 256 characters). Disconnect nulls
the credential columns, sets `disconnected` and keeps the row. Both answer without a token in
the body. Validation errors on `/api/connections` drop `input` (`auth.setup`).

### Withings disconnect and account deletion (#163)

`DELETE /api/connections/withings` (any environment) and `DELETE /api/auth/account` take the
revoke-first path of ADR 0004 section 7. Web makes no Withings call (roadmap Amendment 1):

1. the route enqueues a `withings_disconnect` job (`withings.disconnect.DisconnectInput`:
   the connection id and the id of the session that passed the step-up), at the user lane,
   in a slot of its own: the owner's other jobs (webhook, scheduler, backfill) never block it,
   and at most one disconnect job is queued or running per owner. When none can be queued
   (or the enqueue fails), the route goes straight to step 3; it never answers 429;
2. it waits for the job up to `connections.routes.REVOKE_WAIT_SECONDS` (25 s). The handler
   (`auth.purge.disconnect_kind`) checks the session again (this owner's, live, full, a
   re-authentication and a step-up within ten minutes plus `JOB_SLACK`), then runs
   `disconnect_withings` inside one `provider_call`;
3. if the row is still live afterwards (no job was queued, the job failed before the revoke,
   timed out, or no worker runs the kind), the route cancels a still-queued job, clears that
   row's credentials itself and records `revoke_failed`. Credentials never outlive the
   request. The job clears only its row at the token generation it started with, so a run
   that ends late leaves a reconnected connection alone;
4. `?delete_imported=true` then removes the imported rows, this owner's webhook events (while
   the row still names them) and the disconnected row (`connections.purge.purge_connection`);
   account deletion purges the webhook events and deletes the account
   (`auth.purge.delete_account`).

Both answer 200: `{"status": "disconnected", "revoke_failed", "removal", "imported_deleted"}`
and `{"status": "deleted", "revoke_failed", "removal"}`. `removal` is the code `revoke_incomplete` (the page words it: remove
access in the Withings account), set exactly when `revoke_failed` is; `GET /api/connections`
shows the same as `last_error: "revoke_failed"`. No live Withings row: 409 `not_connected`.
The worker registers the kind (`worker.kinds.worker_registry` calls
`auth.purge.register_disconnect_kind`, a guard 8 entry point); the API's registry stays empty.

The job issuer's account directory (`ownvitals.auth.owners.ACCOUNTS.connection_refs`) reads
live `(provider, id)` pairs in the owner's session. A disconnected row is not a ref.

### Withings connect (#296)

PLAN section 4.3, ADR 0004 section 7, roadmap Amendment 1. `connections.withings_routes`:

- `POST /api/connections/withings/start` (`require_sensitive`, CSRF, the per-IP OAuth start limit
  `OAUTH_IP` that Google's start uses too) answers `{"authorize_url"}` with `no-store`. The state
  comes from `withings.oauth.start`: SHA-256 only, bound to the session id, 10 minutes, one use,
  at most 30 starts per user in 15 minutes (429 `rate_limited`), kept in this web process
  (`app.state.withings_oauth_states`; a restart means starting again, #165 moves it to a table).
  The app is the newest `withings_apps` row; with none the answer is 503
  `withings_not_configured`. A failure to open the app secret is 503 with its category.
- `GET /api/oauth/withings/callback` always answers 303 to `/onboarding?withings=<outcome>`
  (`no-store`, `no-referrer`, empty body); the query (code, state) is never echoed or logged.
  Outcomes: `connected`; `state_rejected` (no full session, a missing, repeated, overlong,
  unknown, expired, spent or foreign state); `denied` (Withings sent `error`: the state is spent);
  `error` (no code: the state is spent); and `withings.oauth.callback`'s refusals
  (`step_up_required` when the step-up lapsed at Withings, `exchange_failed` for a refused or
  failed exchange, a transport error or timeout included, `linked_elsewhere`,
  `other_withings_account`, `conflict`). The state is taken before the exchange, so a replay or a
  foreign try spends it.
- On `connected` the route queues one `withings_connect` job (user lane) for the stored row, in a
  slot of its own (the owner's active jobs plus one) and only when none is queued or running. The
  worker reads devices, goals and capabilities and queues the backfills; if the job cannot be
  queued, the 6-hourly poll still finds the connection. Web makes no other Withings call.

## keys rewrap

`python -m ownvitals keys rewrap`, as the app role (the same engine as web). One transaction
per connection row and per authenticator: unwrap with the row's KEK, wrap with the current
one, `UPDATE ... WHERE key_id = :old`. The payload and `token_generation` stay. A row with
empty credentials is skipped. Output is counts per key id. Run it again after a stop; rows
already on the current KEK are left alone. Exit 1 when a row cannot be unwrapped
(`key_unavailable` leaves it, `decrypt_failed` sets `needs_reconnect`).

Install the new KEK as `connection_kek` with a new `OWNVITALS_CONNECTION_KEK_ID`, keep the old
file as `connection_kek_<old id>`, restart, then rewrap. The old file can go once both counts
for that id are zero (connections and authenticators). The Withings app secret is not this
command's (M1).

## keys wipe

After a suspected KEK compromise, with web and worker stopped and a new KEK id that has no
check row yet:

```bash
docker compose --profile maint run --rm maint python -m ownvitals keys wipe --confirm
```

The maint service uses the owner role. The command refuses while `pg_stat_activity` shows the
app role in this database, and while the configured KEK id already has a check row. It loads
no KEK and calls no provider. Per account, in one transaction: credential columns become null,
one row per provider becomes `needs_reconnect` (any older row of that provider stays
`disconnected`, so the live unique index still holds), and the authenticator is reset the way
`admin mfa-reset` does (TOTP, recovery codes, sessions, known devices). An `auth_events` row
`connections_wiped` records the provider names and whether an authenticator was removed, never
a token. After the commit, a mail says what was removed and where to sign in. It does not
include a token, the address or a provider account id.

Then every `connection_keks` row is deleted, `VACUUM (FULL)` rewrites `connections`,
`mfa_totp`, `mfa_recovery_codes` and `connection_keks`, and the command tries `CHECKPOINT`.
The owner role cannot run `CHECKPOINT`; the refusal prints:

```bash
docker compose exec db psql -U postgres -d ownvitals -c CHECKPOINT
```

`keys wipe --resend` sends mails still marked unsent. It does not wipe again.

## Tests

```bash
cd backend
uvx uv@0.13.6 run pytest -q tests/test_connections.py tests/test_connections_schema.py \
  tests/test_connections_generation.py tests/test_connection_errors.py \
  tests/test_keys_rewrap.py tests/test_keys_wipe.py tests/test_withings_disconnect_routes.py \
  tests/test_withings_connect_routes.py
```

Fixtures are synthetic. The test provider never calls Withings.
