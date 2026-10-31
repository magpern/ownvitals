# ADR 0005: Jobs, leases, fencing and the worker

Status: Accepted (2026-10-31, #4); amended 2026-10-31 by #49 (see "Amendments")

## Context

Everything ownvitals does with Withings happens in the worker (PLAN §2): connect, backfill, sync,
ECG fetch, reconcile, poll and token-refresh recovery (§4.4), and later analysis (§7). PLAN §3
ports classbeat's `jobs/*` and `worker/*` (classbeat ADR 0005) and lists the M1 extensions:
**system and webhook kinds, priorities, singleton keys, rate-limit hold and dead-letter**. §5 says
the queue claim and the reaper go through `SECURITY DEFINER` functions and that workers act
through a `UserContext` issued from the job row.

classbeat ADR 0005 is the base: an owned `jobs` table, five `SECURITY DEFINER` functions owned by
a `NOLOGIN` queue role, leases with a fencing token checked first in every worker transaction,
`LISTEN/NOTIFY` wake-ups and SSE progress. This ADR keeps that core and drops what served
classbeat's product:

- the **approval wait** (`waiting_for_approval`, `expired`, `queue_expire`): no ownvitals job
  waits for a user;
- the **credit seam** (`credit_state`, `queue_unsettled`): ownvitals has no plans (D11); M7 ports
  the per-user AI cost cap without tiers (PLAN §3) and decides then how it meets the jobs;
- **external operations** (classbeat ADR 0005 §7): its uncertain-write machinery protects
  non-idempotent writes into a user's account. ownvitals' upstream writes are
  `notify subscribe/update/revoke` and `oauth2 revoke`, which are safe to repeat and reconciled by
  `notify list` (PLAN §4.4), and the token refresh (`oauth2 requesttoken` with a refresh token),
  which can't be repeated: Withings rotates the refresh token. PLAN §4.3's intent protocol (ADR
  0004 §6) covers that one write, which is why the generic machinery isn't needed.

Health data changes what a stale worker can damage: a sync job that keeps writing after losing
its lease could duplicate or half-write measurements and their revision counters (PLAN §5
provenance). The fence covers those writes too (§4).

#9 ports the core with one test-only kind; M1 adds the Withings kinds and the extensions of §8.

## Decision

### 1. Tables (#9)

Both tables are **owned** (ADR 0002 §2). As in ADR 0004, `id` keeps the `gen_random_uuid()`
default but the application always supplies it. Every column is `NOT NULL` unless marked; every
check is a database `CHECK`.

**`jobs`**

| Column | Notes |
|---|---|
| `id`, `owner_id` | owned |
| `kind text` | `kind ~ '^[a-z_]{1,40}$'`; the code registry (§2) holds the valid kinds |
| `state text` | `queued`, `running`, `completed`, `failed`, `cancelled` (M1 adds `dead_letter`, §8) |
| `input jsonb` | validated per kind; `octet_length(input::text) <= 16384`; ids, stream names and time windows only, **never secrets and never health values** |
| `idempotency_key text`, `request_hash bytea` | for jobs a user creates (§2); key `^[A-Za-z0-9_-]{16,64}$`; `UNIQUE (owner_id, idempotency_key)` |
| `attempt int`, `max_attempts int` | `0 <= attempt <= max_attempts`, `1 <= max_attempts <= 10` (default 3) |
| `available_at timestamptz` | claimable from then on (backoff, holds) |
| `lease_version bigint` | the fencing token, default 0, only ever increases |
| `leased_by text NULL`, `lease_expires_at NULL`, `heartbeat_at NULL` | set exactly while `running` (a check ties them to the state); `leased_by` ≤ 100 characters |
| `cancel_requested_at NULL` | set by the user |
| `error_category text NULL` | from §6's list; set exactly when `failed` |
| `next_seq bigint` | the last `job_events.seq` handed out |
| `created_at`, `updated_at`, `started_at NULL`, `finished_at NULL` | `finished_at` set exactly in a terminal state |

Partial indexes `(available_at) WHERE state = 'queued'`, `(lease_expires_at) WHERE state =
'running'`, and `(owner_id, created_at)`.

**`job_events`**: `id`, `owner_id`, `job_id` (owner-inclusive key to `jobs`, `ON DELETE
CASCADE`), `seq bigint` (`UNIQUE (owner_id, job_id, seq)`), `kind text` (checked against §7's list),
`payload jsonb` (≤ 2048 bytes), `lease_version bigint` (the writer's token, 0 for user writes),
`created_at`. Append-only: restrictive `USING (false)` policies for `UPDATE` and `DELETE`;
cascades still remove the rows with the job or the account.

### 2. States and transitions (#9)

```
queued ──claim──> running ──> completed | failed | cancelled
  ^                 │
  └─retry/reap/─────┘
    hand-back
```

| From → to | By | When / how |
|---|---|---|
| (new) → `queued` | user session | `POST /api/jobs` (below); M1 adds webhook, scheduler and operator origins (§8) |
| `queued` → `running` | `queue_claim` (§3) | only while `attempt < max_attempts`; `attempt + 1`, `lease_version + 1`, new lease |
| `running` → `running` | worker | heartbeat, fenced (§4) |
| `running` → `queued` | worker | retryable error with attempts left, `available_at` = backoff (§6); or shutdown hand-back: fenced, `attempt - 1`, `lease_version + 1`. `cancelled` instead if a cancel was requested |
| `running` → `queued` / `failed` / `cancelled` | `queue_reap` | lease expired: requeue, `failed` with `lease_lost` when attempts are spent, or `cancelled` if requested |
| `queued` → `failed` | `queue_reap` | a queued row with spent attempts (should not exist): `internal`, so one bad row never blocks the queue |
| `running` → terminal | worker | fenced; `cancelled` only after `cancel_requested_at` |
| `queued` → `cancelled` | user session | Cancel, immediately |
| `running` → (cancel requested) | user session | Cancel sets `cancel_requested_at`; the worker stops at its next checkpoint (§6) |

- Every transition is one `UPDATE … WHERE id = :id AND state = :from` (plus the fence for worker
  writes) that also inserts its event; 0 rows means someone moved the job first. **Terminal
  states never change again.**
- **Who acts:** a user session acts only on the user's own jobs (RLS, 404 for a foreign id); the
  worker only on a job it holds the lease for; claim and reap are the only cross-user paths (§3).
- **Kinds:** `ownvitals.jobs.kinds` maps each kind to its input model (Pydantic,
  `extra="forbid"`) and its handler. #9 registers one test-only kind; M1 registers PLAN §4.4's.
- **Creating a job:** `POST /api/jobs` (full session, ADR 0003 §2; CSRF) with an
  `Idempotency-Key` header. In one transaction under `pg_advisory_xact_lock` in the job-creation
  namespace, keyed by the owner (§9; amended 2026-10-31, #49): the
  same key with the same `request_hash` (canonical JSON of the validated body) returns the
  existing job; a different hash is 409 `idempotency_mismatch`; a per-user limit on queued and
  running user-created jobs (a setting, default 2) answers 429; otherwise insert the job and its
  `queued` event and `pg_notify('jobs_queued', '')`. Cancel is idempotent by its target state.

### 3. The queue role and functions (#9)

- **`ownvitals_queue`**: `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS
  NOINHERIT`, member of no role, nobody a member of it. It owns the M0 functions:

| Function | Does |
|---|---|
| `queue_claim(p_worker text, p_lease_seconds int, p_kinds text[])` | checks `p_worker ~ '^[A-Za-z0-9._:-]{1,100}$'` and 30 ≤ `p_lease_seconds` ≤ 600; takes the next claimable job `FOR UPDATE SKIP LOCKED`; sets `running`, the lease and `attempt + 1`, `lease_version + 1`; inserts the `claimed` event; returns `(job_id, owner_id, lease_version)` or nothing |
| `queue_reap(p_limit int)` | for up to `p_limit` (1 to 100) expired leases, `SKIP LOCKED`: `lease_version + 1` (fences the old holder), then requeue with backoff, `failed`/`lease_lost`, or `cancelled`; fails a queued row with spent attempts as `internal`; skips a bad row rather than raising, so one row can't stall other users' claims; returns a count |
| `queue_stats()` | counts per state, no ids; for `ownvitals jobs stats` and the admin area |

- Each sets `search_path = pg_catalog, pg_temp`, schema-qualifies every object, takes no id from
  the caller, and uses the database's `now()`, never a worker clock. `ownvitals_queue` has exactly
  `USAGE` on `public`, `SELECT, UPDATE` on `jobs` and `SELECT, INSERT` on `job_events`;
  `ownvitals_app` has `EXECUTE`; `PUBLIC` nothing.
- Heartbeat and finish are **not** definer functions: they touch only the job the worker already
  holds, so they are fenced writes in the owner's `user_session` (§4), as in classbeat. No
  cross-user privilege is needed for them (Architect decision, 2026-10-31; #9's wording follows).
- **No user code runs as the queue role.** A trigger or rule on any table would run inside a
  queue function with its privileges, so the database holds no user-defined function, procedure,
  aggregate, operator, trigger or rule besides the reviewed ones, and no expression (default,
  check, index) calls a built-in that runs SQL text or changes settings (classbeat ADR 0005 §3 as
  amended). **The allowed database code** (Architect decision, 2026-10-31):
  - the three queue functions above (#9);
  - the admin audit log's append-only guard: its trigger function (`SECURITY INVOKER`, owned by
    `ownvitals_owner`, built-ins only, no SQL text) and its row-level `BEFORE UPDATE OR DELETE`
    trigger on the audit table (#12; classbeat ADR 0011 §1). No queue function touches that
    table, and `ownvitals_queue` has no privilege on it;
  - from #21, `ensure_month_partitions(table, from_month, to_month)` (ADR 0002 §2), owned by
    `ownvitals_owner` (creating partitions requires owning the parent); a guard keeps every
    caller but the scheduler and backfill modules out (amended 2026-10-31, #49: a month range
    and the backfill caller);
  - from M1, the webhook-secret function (ADR 0004 §4), owned by the `NOLOGIN` role
    `ownvitals_webhook_secrets` with ADR 0002 §3's column privileges (amended 2026-10-31, #49;
    it was `ownvitals_owner`): it appends, retires (one entry, or all at once), re-hashes or
    deletes retired entries in the `webhook_secrets` column of one `withings_apps` row under ADR
    0004 §4's input rules, compare-and-clears the rotation request (`rotation_requested_at`,
    `rotation_reason`), and writes the admin-audit row, nothing else;
    `ownvitals_app` may execute it. #21 builds the role and the function; #24's guard keeps
    every caller but the worker's rotation module out.

  Each is pinned the same way: name, owner, `md5(pg_get_functiondef)`, ACL, and for the trigger
  its table, timing and events. M1 adds its function families (webhook ingest and the 30-day
  `webhook_events` purge, scheduler enqueue, rate bucket, and the cancel of a replaced
  rotation's queued `notify update` jobs, ADR 0004 §4; ADR 0002 §3) by amending this list with
  each family's role, grants and digests; anything else is refused.
- **The privileged step.** Handing a function to `ownvitals_queue` needs the superuser, so the
  role, its grants and the functions live in one idempotent script,
  `docker/postgres/queue/20-queue.sql`, run **as the superuser** by the one-shot `db-functions`
  service after `migrate` and before web and worker. Its only secret is `postgres_password`, turned
  into a `PGPASSFILE` at run time (never an environment value). The script refuses unless
  `current_user` is a superuser, creates or repairs the role, revokes and re-grants exactly the
  list above, replaces the functions, sets their owner, revokes `PUBLIC`, grants `EXECUTE` to
  `ownvitals_app` and comments each `ownvitals-queue v<N>`. Migrations never create these
  functions; a migration that changes a column they read bumps `N` in the same PR. CI runs the
  script twice (#9).
- **Startup check:** web and worker extend the role check of ADR 0002 §5. Each function exists
  once, is `SECURITY DEFINER`, is owned by `ownvitals_queue`, has exactly that `search_path`, the
  expected version comment, an `md5(pg_get_functiondef(oid))` equal to the one in
  `ownvitals.db.queue_digests` (regenerated by a CI step against the pinned Postgres image, which
  fails when stale), and exactly the ACL above; the role has exactly its attributes and
  privileges. It runs again on every new pooled connection, and the worker claims nothing until
  it has passed once. Any difference refuses to start, with no override (guard 9).

### 4. From the claim to a `UserContext`; fencing and leases (#9)

- **The job issuer** (ADR 0002 §1) acts on the claimed row alone:
  1. it asks `ownvitals.auth` (guard 8) whether the owner exists and is not disabled; otherwise the
     job fails with `account_disabled`;
  2. it issues a context with `role="user"` (a job never carries admin authority) and the owner's
     connection refs, read through `ownvitals.connections` in that owner's `user_session`;
  3. the job's row and input are then read in a `user_session` with that context.

  The claim returns nothing but the id, owner and token, so there is no ambient authority. **This
  is also the issuer for jobs created by the webhook or the scheduler** (ADR 0002 §4): how a job was
  created never changes how its context is built.
- **Fencing.** Every write a run makes goes through `fenced_session(factory, lease)`: a
  `user_session` whose first statement is

```sql
UPDATE jobs SET updated_at = now()
 WHERE id = :job_id AND owner_id = :owner_id AND lease_version = :v AND state = 'running'
RETURNING cancel_requested_at, next_seq
```

  0 rows raises `LeaseLost` and rolls back. The fence's row lock is held to the commit, so the
  reaper (which skips locked rows) can't bump the token in between. Events, state changes and, from
  M1, the health rows a sync writes together with their `data_revisions` bump and sync cursor (PLAN
  §5) are written in the same fenced transaction, so **a stale worker can write no health data**.
- An AST guard allowlists the modules that may call `user_session` directly (`ownvitals.jobs`,
  `ownvitals.connections`, `ownvitals.auth`; a later owned-data package joins in its own PR);
  worker and kind code use the fenced helper. The one exemption during a run is
  `ownvitals.connections`' status writes, which are conditional on `token_generation` (ADR 0004
  §6) and touch no job data.
- **Leases:** a setting, default 120 s. A heartbeat task extends `lease_expires_at =
  GREATEST(lease_expires_at, now() + lease)` every lease/3 in its own short fenced transaction. A
  failed heartbeat (0 rows, or 3 database errors in a row) marks the run lost: it stops at the next
  checkpoint and starts no further provider call. Before a provider call the run extends its lease
  to at least `now()` + the call's total deadline + 30 s. **No transaction is open across a
  provider call** (ADR 0002 §5 timeouts).

### 5. The worker loop and shutdown (#9)

- `python -m ownvitals.worker` runs up to a configured number of runs at once (default 2) as
  asyncio tasks. When a slot is free it calls `queue_claim`, woken by `LISTEN jobs_queued` on its
  own connection or by a 2 s poll; every 30 s it runs `queue_reap`; the hourly auth purge (ADR 0003
  §1) stays. M1 adds the scheduler's ticks (§8). Several workers are safe: `SKIP LOCKED` gives each
  job to one claimer, and the fence makes a lost lease harmless.
- The worker refuses to start on a privileged database role (ADR 0002 §5), a drifted queue (§3) or
  an invalid KEK ring (ADR 0004 §3).
- **Shutdown:** compose gives the worker `stop_grace_period: 30s`. On SIGTERM it stops claiming,
  starts no new provider call, waits up to 25 s for calls in flight, then hands each lease back
  with a fenced requeue (`attempt - 1`, `lease_version + 1`, event `requeued`; `cancelled` if a
  cancel was requested), so a deploy costs no attempt. What isn't handed back is reaped when its
  lease expires. A database role error from any connection stops the worker the same way and is
  never a job's failure.

### 6. Retries and cancellation (#9)

| Category | Retry | From |
|---|---|---|
| `upstream_unavailable` | yes | ADR 0004 §8; also our own database's transient errors |
| `rate_limited` | yes, at least its hint | ADR 0004 §8; becomes a hold without an attempt in M1 (§8) |
| `lease_lost` | yes (reaper) | §3 |
| `not_connected`, `needs_reconnect`, `key_unavailable`, `decrypt_failed` | no | ADR 0004 §3, §5, §8 |
| `invalid_input`, `account_disabled` | no | §2, §4 |
| `outbound_blocked`, `internal` | no | the egress allowlist (#6); anything else |

- Retryable: back to `queued` with `available_at = now() + min(30 s · 2^(attempt-1), 15 min)`,
  ±20 % jitter. Non-retryable, or attempts spent: `failed` with the category. The user sees one
  plain sentence per category, never error text.
- **Cancellation** is checked at checkpoints (before each provider call, between pages and
  windows of a sync, at each heartbeat through the fence's `cancel_requested_at`); a requeue of a
  job with a cancel requested ends it `cancelled`. Kind code never nests a fenced transaction in
  another: it would wait on the fence's own row lock until `lock_timeout`.

### 7. Progress: events and SSE (#9)

- **Event kinds:** `queued`, `claimed {attempt}`, `progress {done, total}`, `retry_scheduled
  {category, available_at}`, `cancel_requested`, `cancelled`, `completed`, `failed {category}`,
  `requeued`; M1 adds per-stream backfill progress (counts and the covered date range). Each kind
  has a Pydantic model with `extra="forbid"`. Payloads hold only enum values, ids of the owner's
  own rows, counts and dates of the job's own progress. **Never** tokens, error or provider text,
  measurement values, ECG samples or finding content.
- **SSE:** `GET /api/jobs/{id}/events` (full session; 404 for a foreign or unknown id). Each event
  is `id: <seq>`, `event: <kind>`, `data: <payload>`; `Last-Event-ID` replays later events. Every
  read is a short `user_session`. A comment line every 15 s keeps proxies from closing an idle
  stream; the server ends a stream after 10 minutes and after a terminal event, and the browser
  reconnects; a reconnect to an ended job with nothing new gets 204. At most 5 open streams per
  user (429 beyond). `GET /api/jobs/{id}` returns the state for clients without SSE.
- **Behind SWAG and Cloudflare** (§9.2): SWAG's location must not buffer this path (the owner's
  SWAG configuration, M2); the keep-alive comment stays well under the proxies' idle limits.
- **Wake-up:** the transaction that inserts an event runs `pg_notify('job_events', <job id>)`,
  only the id. Each web process has one dedicated `LISTEN` connection that never queries owned
  tables; a woken stream reads in a user session, so a notification about someone else's job
  reveals nothing. If the listener is down, streams poll every 5 s.

### 8. M1 extensions (constraints for M1's design)

PLAN §3 and §4.4 name them; M1's issues and ADR detail them within these rules:

- **Origins.** Jobs gain an `origin`: `user` (`POST /api/jobs`, e.g. "sync now"), `webhook` (the
  ingest function, ADR 0002 §4), `scheduler` (enqueue functions for poll and reconcile) and
  `operator` (`ownvitals jobs replay`, audited). Only `user` jobs carry an idempotency key; its
  unique index becomes partial and keeps `owner_id`. Webhook and scheduler jobs are created only
  inside definer functions and get their context only when claimed (§4).
- **Kinds** (PLAN §4.4): `withings_connect`, `withings_backfill` (resumable, lowest lane),
  `withings_sync`, `ecg_fetch`, `withings_reconcile`, `withings_poll`, `token_refresh_recover`,
  each with its retry budget and terminal category.
- **Singleton keys.** `singleton_key text NULL` with a unique index on `(owner_id, singleton_key)
  WHERE state = 'queued'`: at most one queued job per key (for example per connection and stream).
  A trigger while one is queued collapses into it (the 30 s debounce sets its `available_at`); a
  trigger while one is running queues exactly one follow-up. Duplicate and replayed webhooks
  therefore never multiply work (PLAN §4.4).
- **Priorities and fairness.** A lane per job (user "sync now" > webhook > poll > reconcile >
  backfill) and a per-owner fairness key in `queue_claim`'s order (round-robin, each user's backfill
  share capped). That changes `queue_claim` and bumps its version.
- **Rate-limit hold.** A run that can't get a rate token, or meets a paused bucket (status 601 →
  `wait_seconds` or 60 s), goes back to `queued` with `available_at` at the bucket's resume time
  **without spending an attempt**, like a hand-back. Tokens count as consumed when claimed and are
  never refunded; concurrency slots carry a lease (PLAN §4.4). The bucket is a system table behind
  its own definer functions.
- **Dead-letter.** A job whose retry budget is spent on retryable failures ends in `dead_letter`, a
  terminal state distinct from `failed` and counted in the metrics (PLAN §4.4). `ownvitals jobs
  replay <id>` creates a **new** job with the same kind and input; the terminal row never changes.
- **Webhook quotas** per connection and per app (PLAN §4.4) are enforced inside the ingest
  function; over quota, the event is recorded and no job is added (Withings still gets 200; the
  poll catches up).
- **Owner-less upkeep** (retention purges such as the 30-day `webhook_events` purge, creating
  partitions ahead through `ensure_month_partitions` (ADR 0002 §2), secret rotation scheduling
  and picking up a rotation request in the order its `rotation_reason` names (ADR 0004 §4;
  amended 2026-10-31, #49)) runs as maintenance tasks in the worker loop, like reap and purge,
  never as queue rows without an owner: `jobs` stays an owned table.
- **Past months for a backfill** (amended 2026-10-31, #49): a `withings_backfill` run calls
  `ensure_month_partitions` for its window's months before inserting the window, in a short
  fenced transaction of its own, never in the transaction that inserts the window (ADR 0002 §2).

### 9. Advisory-lock namespaces (amended 2026-10-31, #49)

Every advisory lock uses the two-key form, `(namespace, key)` with two `int4`s, and takes its
namespace from this registry, so features can't collide. The constants live in one module, and
the SQL in `db-functions` uses the same numbers. A UUID key is hashed to `int4`
(`hashtext(id::text)`); a hash collision only makes one caller wait.

| Namespace | Lock | Key | Where |
|---|---|---|---|
| 1, job creation | `pg_advisory_xact_lock` | the owner id, hashed | §2 |
| 2, token refresh | `pg_advisory_lock` (session-scoped) | the connection id, hashed | ADR 0004 §6 |
| 3, partitions | `pg_advisory_xact_lock` | the parent table's oid, as `int4` | ADR 0002 §2 |

A new advisory lock adds its row here in the PR that introduces it.

## Alternatives

- **A system-table queue without RLS**, or a separate queue database: workers could scan
  everyone's jobs; two sources of truth (classbeat ADR 0005).
- **Advisory locks instead of leases:** they can't fence writes made on another connection and
  hide state from the reaper and the UI.
- **Redis or a broker:** no new infrastructure (D6); `SKIP LOCKED` is enough at this scale and keeps
  job state transactional with the health data it writes.
- **Porting classbeat's external operations:** its value is for writes that can't be repeated;
  ownvitals has none (Context).
- **A separate queue table per origin or kind:** one table with an origin column keeps one claim
  path, one fence and one set of guards.
- **WebSockets for progress:** SSE is one-way and resumes with `Last-Event-ID`.

## Consequences

- #9 ports the tables, the three functions, `db-functions`, the startup check, the worker loop, SSE
  and the jobs API, and proves: one job per claimer under concurrency; a stale holder's writes
  refused after a reap; idempotent queue SQL with matching digests; jobs isolated in the worker
  context. #15 extends the isolation tests to the webhook and scheduler contexts.
- Compose gains `worker` and `db-functions`; the worktree script and CI run `20-queue.sql` after
  migrations. The superuser password is mounted only on `db` and `db-functions`.
- Every M1 kind reuses the lease, fence and event rules unchanged; M1 amends §3 for its new
  function families and adds §8's columns with a queue version bump.
- Threat model T44 to T51 cite this ADR.

## Amendments

### 2026-10-31 (#49): follow-ups of the #41 review

Architect decisions, made in place above and marked "amended 2026-10-31, #49":

- §3: the webhook-secret function is owned by `ownvitals_webhook_secrets` instead of
  `ownvitals_owner`, may also retire all entries at once, delete retired ones and
  compare-and-clear the rotation request, and enforces ADR 0004 §4's input rules;
  `ensure_month_partitions` takes a month range and has two callers, the scheduler and backfill
  modules; M1's list gains the cancel of a replaced rotation's queued jobs.
- §2, §9: every advisory lock uses the two-key form with a namespace from §9's registry.
- §8: a backfill creates the past months its window needs, in a short transaction before the
  window's write; the upkeep picks up a rotation request in the order its reason names.
