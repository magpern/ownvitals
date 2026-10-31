# ADR 0002: Tenant isolation: UserContext, forced RLS, roles and execution contexts

Status: Accepted (2026-10-31, #4); amended 2026-10-31 by #49 (see "Amendments"). Its two open
questions were decided by the Architect on 2026-10-31 (§2 partitions, whose keys
[roadmap Amendment 2](../roadmap.md) sets; §3 `webhook_events` retention).

## Context

Every measurement, ECG, sleep record, finding and score belongs to one user. PLAN §5 makes the
ownership rules explicit (`owner_id`, `UNIQUE (owner_id, id)`, owner-inclusive composite foreign
keys, forced RLS, a `UserContext` issued from the job row, `SECURITY DEFINER` functions for
cross-user work, no `BYPASSRLS` role in any ordinary path), and §3 ports classbeat's isolation
layer with two changes: an issuer for webhook- and scheduler-triggered jobs, and RLS tests in
every execution context.

Health data raises the stakes. A missed filter here discloses special-category data (GDPR Art. 9)
about another person, in practice usually another household member. The household exemption of
§9.3 depends on the processing, not on the code, so it lowers no technical bar. classbeat ADR 0002
is the base; this ADR keeps its contract and records what changes:

- more execution contexts than web and worker: the Withings webhook, the scheduler, the admin
  area and the operator CLI (§4);
- jobs created without any user session (webhook, scheduler), which get their context only when
  claimed (§4);
- one sanctioned global unique index, `connections (app_id, withings_userid)` (§2);
- the admin role has no access to health data at all (§4, §5);
- partitioned health tables, which classbeat forbids, allowed here under the rules of §2, with
  their own key shape (roadmap Amendment 2).

## Decision

### 1. UserContext

```python
@dataclass(frozen=True, slots=True)
class UserContext:
    user_id: UUID
    role: Literal["user", "admin"]
    connections: frozenset[tuple[str, UUID]] = frozenset()  # (provider, connection row id)
```

- Lives in `ownvitals.context`; immutable, request- or job-scoped, never stored globally,
  hashable (a test checks `hash(ctx)`). `connections` holds references, never credentials.
  classbeat's `entitlements` field (plans and tiers) is dropped: ownvitals has none (D11).
- **Exactly two issuers**, as in classbeat:
  - `ownvitals.auth`, from the authenticated server-side session, and **only after the second
    factor** (ADR 0003 §2): a session that has not passed TOTP never yields a context;
  - `ownvitals.jobs`, from the job row the worker claimed (ADR 0005 §4), always with
    `role="user"`.

  The operator CLI's per-owner work (rewrap, wipe) gets its contexts from an `ownvitals.auth`
  function used only by the CLI (classbeat ADR 0004 §1). Tests use a test-only helper.
- **Runtime barrier** (unchanged from classbeat ADR 0002 §1): construction needs a module-private
  issuer token compared by identity before any field is set; `UserContext(...)`, `type(ctx)(...)`,
  `ctx.__class__(...)`, `dataclasses.replace`, a repeated `ctx.__init__` and pickling are refused;
  `require_context(ctx)` (called by every function that opens database access with a context)
  accepts the exact class only; `__get_pydantic_core_schema__` raises, so no request model, route
  parameter or `TypeAdapter` can build one from request data.
- **AST guard** (guard 5): outside `ownvitals.auth`, `ownvitals.jobs` and the tests, any reference
  to `UserContext` other than an annotation, any reference to the token, `Depends(UserContext)`, a
  bare `Depends()` for a context parameter, and anywhere `dataclasses.replace`,
  `object.__setattr__`, `object.__new__`, `x.__class__(...)` and `type(x)(...)` fail CI.
- These stop accidents and make misuse visible in review; deliberately malicious code inside the
  process is the review gate's job, not the barrier's (classbeat's scope statement holds).
- Handlers get the context through one FastAPI dependency. **A user id never comes from a path,
  query, body, header, webhook payload or model output.** Ids from the browser are looked up
  within the owner's rows; not found and not owned are the same answer (404).

### 2. Owned tables

Every table holding per-user data is **owned**. That includes every health table of PLAN §5
(`sync_cursors`, `devices`, `measure_groups`, `measures`, `ecg_recordings`, sleep, activity,
workout and intraday tables, `observations`, `metric_daily`, `findings`, `scores`,
`data_revisions`), `connections` (ADR 0004), and the job queue and its events (ADR 0005).

- `owner_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE` (from #10, when `users`
  exists). Account deletion therefore removes every owned row; Postgres runs referential actions
  without row security, so the cascade works under `FORCE`.
- `id uuid` with the server default `gen_random_uuid()`. On every owned table except the
  partitioned ones below, `id` is the primary key and `UNIQUE (owner_id, id)` lets children
  reference it.
- **Every other unique constraint or unique index includes `owner_id`**, and every exclusion
  constraint has `owner_id WITH =`, because both are checked without RLS and a global one leaks
  which values other users hold.
  **The one sanctioned exception** is `connections (app_id, withings_userid)` through generated
  columns (PLAN §5), which PLAN §4.3 needs to refuse linking a Withings account that is already
  linked to another ownvitals account. It reveals only that a Withings account the caller has
  just proved control of (by completing its OAuth flow) is linked elsewhere. Guard 1 allows
  exactly that index, by table, name and column list; any other owner-less unique index fails.
- **Foreign keys between owned tables include the owner**:
  `FOREIGN KEY (owner_id, connection_id) REFERENCES connections (owner_id, id)`. `ON DELETE` is
  `CASCADE`, `RESTRICT`/`NO ACTION`, or `SET NULL (<col>)` with a column list, never a bare
  `SET NULL` (it would null `owner_id`).
- No foreign key from a system table into an owned table (§3).
- RLS through the migration helper `ownvitals.db.rls.enable_owner_rls(table)`:

```sql
ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <t> FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON <t>
  AS PERMISSIVE FOR ALL TO PUBLIC
  USING      (owner_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
  WITH CHECK (owner_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
```

- No setting means **no rows**, never an error and never everything (`NULLIF` covers both the
  unset and the ended-transaction cases). `FORCE` applies the policy to the table owner too.
- `owner_isolation` is the only permissive policy on an owned table; any further policy is
  `AS RESTRICTIVE` (append-only event tables use that, ADR 0005 §1).
- Repositories still filter `owner_id = ctx.user_id`; RLS is the second layer.
- Owned tables live in schema `public`; inheritance is never used. classbeat forbids partitioned
  owned tables. PLAN §5 requires monthly partitions for `observations`, `sleep_series` and
  `intraday`, "created 3 months ahead by the scheduler". Three things set them apart: a partition
  queried directly is governed by its own RLS settings, not the parent's; adding a partition is
  DDL that only the table owner may run; and PostgreSQL requires every primary key and unique
  constraint on a partitioned table to include the partition key, so `UNIQUE (owner_id, id)`
  can't exist there. **A partitioned owned table is allowed only when** (Architect decision,
  2026-10-31; the key rule is **[roadmap Amendment 2](../roadmap.md)**):
  1. its key is **`PRIMARY KEY (owner_id, id, <partition key>)`** (for example
     `(owner_id, id, ts)`) in place of `UNIQUE (owner_id, id)`; any other unique constraint on it
     includes `owner_id` and the partition key. `id` alone is therefore not unique across
     partitions, so ingest deduplicates by a natural key that includes the time column (for
     example `(owner_id, connection_id, <stream fields>, ts)`), never by `id`;
  2. **no foreign key references it**: a partitioned table is a leaf that nothing points at, and
     guard 1 refuses a foreign key whose target is one. Its own foreign keys stay
     owner-inclusive, for example `(owner_id, connection_id) → connections (owner_id, id)`;
  3. the parent has RLS enabled and forced, with the `owner_isolation` policy above;
  4. every partition also has RLS enabled and forced, with the same policy (defence in depth);
  5. `ownvitals_app` has privileges on the parent only: each partition gets `REVOKE ALL` from it
     (the owner's default privileges would otherwise grant them), so the app reaches rows only
     through the parent and its policy;
  6. partitions are created only by **one reviewed `SECURITY DEFINER` function owned by
     `ownvitals_owner`**, `ensure_month_partitions(table, from_month, to_month)` (amended
     2026-10-31, #49: a backfill writes into past months, which PLAN §5's "3 months ahead"
     doesn't cover). It accepts only an allowlisted table and bounds the range itself: from no
     earlier than the oldest month any backfill window reaches (#25) to no later than now + 3
     months. It creates the missing monthly partitions and applies 4 and 5 to each in the same
     transaction. It is installed and pinned like the other definer functions (ADR 0005 §3).
     Web and worker share the `ownvitals_app` role, so "the worker only" is enforced in code:
     only the scheduler module (the months ahead, §4) and the backfill module (the months of a
     window, before inserting it; #25) call it, and #21's guard fails on any other module that
     names it. Creating a partition locks the parent against every reader and writer until the
     commit, so each caller runs it in a short transaction of its own, never in the transaction
     that inserts a window.

  Until #21 lands, guard 1 refuses partitioned tables. #21, the first issue that creates one,
  extends guard 1 to check rules 1 to 5 for every partitioned table and its partitions instead
  of refusing them (not #5).

### 3. System tables and `SECURITY DEFINER` functions

**System tables** are not per-user or must be read before a user is known. They are listed in
`ownvitals.db.SYSTEM_TABLES` with a one-line reason each and have no `owner_id` column.

| When | System tables |
|---|---|
| M0 | the account tables (ADR 0003 §1, reached only through `ownvitals.auth`, guard 8); the KEK check table (ADR 0004 §3); stored flags and the admin audit log (#12) |
| M1 | `withings_apps` (below); `webhook_events`; the per-app rate bucket (PLAN §4.4) |

- `withings_apps` holds each app's configuration (client id, enveloped client secret, callback
  base; ADR 0004 §2) and its `webhook_secrets` entries (ADR 0004 §4). `ownvitals_app` gets
  **column-level `SELECT` only**, on the columns web and worker need: web reads the client secret
  for the code exchange (roadmap Amendment 1) and the entries to verify a webhook; the worker
  reads the client secret for refresh and revoke and the entries to build callback URLs. The
  role is shared, so who calls what is enforced by egress (ADR 0004 §7) and the crypto guard,
  not by grants. **No runtime role writes the table** (amended 2026-10-31, #49):
  `ownvitals_app` and `ownvitals_maint` (the other login web holds, §5) have no `INSERT`,
  `UPDATE` or `DELETE` on it, so no runtime process can change the stored configuration
  (guard 11). `ownvitals_maint` may `SELECT` only the non-secret columns, for the admin view:
  never the enveloped client secret or `webhook_secrets`. That is how this ADR reads PLAN §5's
  "(system, admin-only)": admins may view the non-secret columns, and only the operator may
  change them; the admin area has no write path to the table. The writes are split (Architect
  decisions, 2026-10-31):
  - **app configuration:** only #22's command-line tool, connected as `ownvitals_owner`, whose
    DSN is mounted only in one-off operator containers (as for migrations), never in web or
    worker. It writes the row and its admin-audit entry in one transaction. The same tool, in
    the same kind of container, rewraps the enveloped client secret after a KEK rotation (ADR
    0004 §3), and its `rotate` command only sets `rotation_requested_at`, which asks the worker
    to rotate (ADR 0004 §4);
  - **`webhook_secrets` entries:** only the worker's rotation, through the webhook-secret
    function of ADR 0004 §4 (append, retire, re-hash or delete retired entries of one row, clear
    `rotation_requested_at`, plus the audit row; no other column).
- `webhook_events` holds a Withings user id, an `appli` and a measurement time window: health
  metadata. `ownvitals_app` gets **no privilege on it at all** (the default grants are revoked in
  its migration); only the ingest function's role reads and writes it (§4). Its `job_id` is a
  plain column, not a foreign key (no foreign key from a system table into an owned table).
  **Rows are kept 30 days, then purged by the scheduler** (Architect decision, 2026-10-31)
  through a definer function of the webhook family (ADR 0005 §3), well beyond Withings' retry
  schedule of about 5 hours (withings-findings §2) that the dedupe key must outlive. The rows
  carry a Withings user id rather than an `owner_id`, so #27's account-deletion test covers the
  table explicitly.
- **Cross-user operations go only through narrow `SECURITY DEFINER` functions** (PLAN §5: queue
  claim, reaper, webhook ingest, rate bucket). Rules, from classbeat ADR 0002 §3 as amended by its
  ADR 0005 §3:
  - each function family is owned by a dedicated `NOLOGIN` role with `BYPASSRLS` and only the
    privileges its functions need; where a table holds secrets, the grants are **column-level**
    and exclude every secret column (the ingest role may read `connections`' id, owner, app id,
    Withings user id and status, never `secret`, `wrapped_dek` or the token columns);
  - `SET search_path = pg_catalog, pg_temp`, every object schema-qualified;
  - no owner id is taken from the caller; inputs are validated in the function;
  - the minimum is returned: ids and a lease token for the queue claim, an outcome enum for
    webhook ingest, counts for maintenance;
  - `ownvitals_app` gets only `EXECUTE`; `PUBLIC` gets nothing;
  - functions are installed by the superuser script of ADR 0005 §3 (`db-functions`), never by
    migrations, and pinned by `md5(pg_get_functiondef)` in a startup check; the database holds no
    other user-defined function, trigger or rule (ADR 0005 §3). Each new family is added by an
    amendment to ADR 0005 §3 with its role, grants and digest.
- **One exception to the owner rule:** the partition function of §2 is owned by
  `ownvitals_owner` (which has no `BYPASSRLS`), because creating or attaching a partition
  requires owning the parent table. It runs only fixed DDL on allowlisted tables and never reads
  or writes rows; callers: the scheduler and backfill modules (a guard checks it).
- **The webhook-secret function** of ADR 0004 §4 has its own narrow owner (amended 2026-10-31,
  #49; it was `ownvitals_owner`, whose rights a bug in it would have carried: DDL, and turning
  off forced RLS on every health table). Its owner `ownvitals_webhook_secrets` is a dedicated
  `NOLOGIN` role, as the rule above, but without `BYPASSRLS`, since it touches only system
  tables. It holds `UPDATE` only on `withings_apps (webhook_secrets, rotation_requested_at)` and
  `INSERT` on the admin audit table, plus the column-level `SELECT` that PostgreSQL requires for
  what those statements read: `withings_apps (id, webhook_secrets)` and `connection_keks
  (key_id)` (ADR 0004 §4's known-id rule); never a secret column. It is pinned and installed
  like the others; caller: the worker's rotation module (a guard checks it).

### 4. Execution contexts

| Context | Process, DB role | Reaches owned rows through | Context issued by | RLS tests |
|---|---|---|---|---|
| Web request | web, `ownvitals_app` | `user_session(ctx)` | `ownvitals.auth`, after TOTP | #5, #15 |
| Webhook (M1) | web, `ownvitals_app` → `EXECUTE` on the ingest function | nothing directly; the function writes the event and the job | none: the job gets one when claimed | #15 (harness), M1 |
| Worker job | worker, `ownvitals_app` | fenced `user_session(ctx)` (ADR 0005 §4) | `ownvitals.jobs`, from the claimed row | #9, #15 |
| Scheduler | worker, `ownvitals_app` → `EXECUTE` on enqueue functions | nothing directly; the functions enqueue owned jobs | none: the job gets one when claimed | #15 (harness), M1 |
| Maintenance (reap, purge) | worker, definer functions or `system_session` on system tables | definer functions only | none | #9, #15 |
| Admin | web, `ownvitals_maint` in an audited transaction | column-level allowlist; **no health table** | `ownvitals.auth` (admin session with TOTP) | #12, #15 |
| Operator CLI | `docker compose exec`/`run` on the host | per-owner `user_session`; `keys wipe`, the `withings_apps` configuration and its client-secret rewrap as the owner role, in one-off operator containers (ADR 0004 §3, §3 above) | an auth function used only by the CLI | #13, #22 |

- **No context has ambient authority.** Every owned read or write happens in a `user_session` for
  one owner, or inside a reviewed definer function. A function needing two users' data is a design
  error; any cross-user aggregate (population statistics, household sharing) needs its own ADR.
- **Webhook** (PLAN §4.4): the receiver checks the secret and validates the fields, then calls the
  ingest function, which in **one transaction** inserts the `webhook_events` row (dedupe key),
  resolves `(app_id, withings_userid)` to the connection and its owner, and inserts or collapses
  the singleton sync job for that owner (ADR 0005 §8). It returns an outcome only (`queued`,
  `duplicate`, `unknown_user`, `over_quota`), never an owner or connection id. The web process
  never holds a `UserContext` for a webhook, and the payload never names an owner.
- **Scheduler** (PLAN §4.4 poll and reconcile): definer functions enqueue the due jobs for every
  connection that needs one and return counts. Owner-less upkeep runs as maintenance through its
  own narrow function or on system tables: creating partitions ahead through
  `ensure_month_partitions` (§2), the 30-day `webhook_events` purge through its definer function
  (§3), other retention purges and KEK checks.
- **The issuer for webhook- and scheduler-triggered jobs is the job issuer.** Such jobs are created
  without a context and get one only when claimed, from the claimed row alone, exactly like a
  job a user created (ADR 0005 §4: the owner must exist and be active; `role="user"`; connection
  refs read in that owner's session). There is no third issuer, and no path by which a webhook or
  the scheduler hands an owner id to application code.
- **Admin**: `ownvitals_maint` (#12) has `BYPASSRLS`, read-only transactions by default, and
  column-level `SELECT` on an allowlist. **ownvitals adds a rule classbeat does not have: the
  allowlist contains no column of a health-data table.** Of the owned tables of §2 it may read
  only status and metadata columns of `connections` and of the job tables, never their secret
  columns or job input; of `withings_apps` only the non-secret columns, and it writes none of
  it (§3). Admins see accounts, connection status and job state, never measurements, ECG,
  observations, findings, scores, raw payloads or webhook events. Guard 11 (#12) enforces it
  with a registry of forbidden tables and secret columns.
- **Operator CLI**: trusted (it needs host access) but uses the same contexts; `jobs replay` (M1)
  and `probe` (M1, shapes only, §10.4) are audited.

### 5. Database roles

| Role | Login | Used by | Rights |
|---|---|---|---|
| `postgres` (superuser) | yes | the db init script and `db-functions` (ADR 0005 §3) only | creates roles, database and the definer functions |
| `ownvitals_owner` | yes | `migrate` (Alembic), `keys wipe` (ADR 0004 §3) and #22's `withings_apps` configuration and client-secret rewrap, all in one-off operator containers | `NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`; owns the database, schema `public`, every table and (from #21) the partition function of §3; member of no `BYPASSRLS` role; **its login is never given to web or worker** |
| `ownvitals_app` | yes | web and worker | `NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT`, member of no role, owns nothing; the allowlist below |
| `ownvitals_queue` (M0), M1's ingest, scheduler and rate roles | no | `SECURITY DEFINER` functions | `BYPASSRLS`; only the privileges their functions need (§3) |
| `ownvitals_webhook_secrets` (M1) | no | the webhook-secret function (§3) | `NOBYPASSRLS`; exactly the column privileges and audit `INSERT` of §3 |
| `ownvitals_maint` | yes | the admin area (#12) | `BYPASSRLS`; column-level allowlist without health tables; of `withings_apps` the non-secret columns only, and no write; default read-only; every use audited |

- `ownvitals_app`'s grants are an **allowlist**: `CONNECT` on its database, `USAGE` on `public`,
  `SELECT, INSERT, UPDATE, DELETE` on tables and `USAGE, SELECT` on sequences through default
  privileges of `ownvitals_owner`, minus explicit revokes (`webhook_events`; `withings_apps`,
  whose table privileges give way to §3's column-level `SELECT`), and
  `EXECUTE` on the definer functions meant for it. Never `TRUNCATE` (it ignores RLS),
  `REFERENCES`, `TRIGGER`, `MAINTAIN`, `CREATE` or `TEMP`.
- `statement_timeout` 30 s, `lock_timeout` 5 s and `idle_in_transaction_session_timeout` 60 s are
  role defaults of `ownvitals_app`. Web and worker check at startup and on every new pooled
  connection that these are exactly its role-level settings, and refuse otherwise (injected SQL
  could change them; this detects it).
- `PUBLIC` loses `CONNECT` on every database, `TEMP`, `USAGE`/`CREATE` on `public` and default
  `EXECUTE` on functions; roles get what they need explicitly.
- **Web and worker refuse to start** if their role is a superuser, has or inherits `BYPASSRLS`,
  belongs to `pg_execute_server_program`, `pg_read_server_files` or `pg_write_server_files`, or
  owns any table, schema or the database; checked at startup and on every new pooled connection;
  no override (guard 6).
- "No `BYPASSRLS` role in any ordinary path" (PLAN §5) holds: web requests, webhooks, worker jobs
  and the scheduler run as `ownvitals_app`. `BYPASSRLS` exists only on `NOLOGIN` function owners,
  reachable through reviewed functions, and on `ownvitals_maint`, which is audited and sees no
  health data.
- Data migrations on owned tables see no rows under `FORCE`; each states how it reaches them:
  per owner with `app.user_id` set, or as `ownvitals_maint` with an audit row for a table maint
  may read. Health tables are always migrated per owner, since maint can't read them.
- Roles come from `docker/postgres/init/10-roles.sql`, idempotent, passwords from secret files
  (#5; CI runs it twice).

### 6. Sessions

`ownvitals.db.session` (#5), as classbeat ADR 0002 §5:

- `user_session(factory, ctx)` begins a transaction, runs
  `SELECT set_config('app.user_id', :user_id, true)` (transaction-local), commits on success and
  rolls back on error. It owns the transaction: `commit()` or `rollback()` inside it raises.
  `system_session(factory)` reaches system tables only; owned tables return no rows there.
- Never a session-level `SET app.user_id`. The pool resets connections with a rollback and
  `DISCARD ALL` (dropping held cursors, temp tables, prepared statements, advisory locks and
  session settings), invalidates a connection whose reset fails, and checks on checkout that
  `app.user_id` is unset and the timeouts equal the role defaults.
- One transaction per `user_session`. Long-lived work holds no session: an SSE stream opens a short
  session per read; `LISTEN` uses a dedicated connection that never queries owned tables;
  `NOTIFY` payloads carry ids only (ADR 0005 §7).
- An AST guard allowlists the modules that may open `user_session` or `system_session` directly
  (#5 `session_scan.py`); worker code writes only through the fenced helper of ADR 0005 §4.

### 7. Guards (kept green forever)

| # | Checks | Issue |
|---|---|---|
| 1 | Every table in `public` is in `SYSTEM_TABLES` (no `owner_id`) or owned: `owner_id NOT NULL`, forced RLS, exactly the one permissive policy, `UNIQUE (owner_id, id)`, owner-inclusive unique indexes (the one exception of §2 allowed by name) and foreign keys, no exclusion constraint without `owner_id WITH =`, `ON DELETE` as §2, `id uuid` with `gen_random_uuid()`; no inherited table; no view in `public`; no foreign key from a system to an owned table. A partitioned table is refused until #21 extends the check to §2's partition rules 1 to 5: `PRIMARY KEY (owner_id, id, <partition key>)` instead of `UNIQUE (owner_id, id)`, no foreign key referencing it, and forced RLS, the policy and no app privilege on every partition (roadmap Amendment 2) | #5; partitions #21 |
| 2 | `ownvitals_app`'s privileges equal the allowlist of §5 (catalog-wide, as an allowlist, so a new privilege kind fails); nothing granted to `PUBLIC` beyond Postgres' built-ins; `ownvitals_owner` has no `BYPASSRLS` | #5 |
| 3 | On test-only owned parent and child tables, two users: no cross-user read, update, delete, insert or child-to-foreign-parent reference; no rows without a context; the app role can't disable RLS; a global-looking unique value can't be probed | #5 |
| 4 | A pooled connection carries no `app.user_id` after a `user_session`; a `WITH HOLD` cursor can't be fetched by the next user; `commit()` inside `user_session` raises | #5 |
| 5 | The `UserContext` AST check (§1) | #5 |
| 6 | Startup refusal of a privileged role (§5) | #5 |
| 7 | Another user's id gives 404 on every owned endpoint (from #10 on, per route) | #10 onward |
| 8 | Only `ownvitals.auth` touches the account tables (ADR 0003 §1) | #10 |
| 9 | The queue role and functions (ADR 0005 §3) | #9 |
| 11 | `ownvitals_maint` holds exactly its allowlist; no secret column and no health table readable. From #21: no runtime role (`ownvitals_app`, `ownvitals_maint`) holds a write privilege (`INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`) on `withings_apps`, and maint reads only its non-secret columns (§3) | #12; `withings_apps` #21 |
| ctx | Every owned table has a test in every execution context of §4 (enumerates tables with `owner_id`) | #15 |

classbeat's guard 10 (retention registry) is not ported in M0, and guard 1 does not check the
`users` cascade. #27 (M1, disconnect and account delete) adds an account-deletion test that
checks every owned table's `owner_id` references `users (id) ON DELETE CASCADE`, plants a row in
every owned table and every system table holding user-linked rows (`webhook_events` included),
and shows each gone after the deletion (threat model T70). In CI a missing test database fails
these tests instead of skipping.

## Alternatives

- **Application filtering only**: one forgotten filter discloses health data; no second layer.
- **A schema or database per user**: strong, but migrations, pooling and the cross-user queue and
  rate bucket get much harder; RLS isolates rows in one schema.
- **Session-level `SET`**: leaks across pooled connections.
- **A third issuer for webhook and scheduler jobs**: it would give the web process or the
  scheduler a way to hold an arbitrary owner's context. Creating the job in a definer function and
  issuing the context only at claim keeps exactly two issuers.
- **Admin reads health data through `ownvitals_maint`** (classbeat lets maint read allowlisted
  class data): rejected; nothing in the admin area needs health values, and audited access is
  still access to special-category data.
- **Owner-inclusive unique key on `(owner_id, app_id, withings_userid)`**: it would let two
  ownvitals accounts link the same Withings account, which PLAN §4.3 forbids.

## Consequences

- Every per-user table needs `owner_id`, `UNIQUE (owner_id, id)` (or, if partitioned,
  `PRIMARY KEY (owner_id, id, <partition key>)`), owner-inclusive keys and `enable_owner_rls`, or
  guard 1 fails the PR; its `users` cascade is checked by #27's deletion test (§7).
- #21 adds the first partitioned tables, `ensure_month_partitions`, the scheduler call, the
  caller guard and the guard 1 extension for partitions, together; #25 adds the backfill call.
  Nothing may reference `observations`,
  `sleep_series` or `intraday`; a design that needs to point at one of their rows needs a new
  ADR.
- Webhook and scheduler work exists only as definer functions plus owned jobs; M1 adds those
  functions through amendments to ADR 0005 §3 (role, grants, digest).
- `ownvitals_maint`'s allowlist never grows to a health table without a new ADR.
- SQL injection would still defeat both layers (the app role can set `app.user_id`): only bound
  parameters, no SQL built from strings (threat model T24).
- Threat model T19 to T28 cite this ADR.

## Amendments

### 2026-10-31 (#49): follow-ups of the #41 review

Architect decisions, made in place above and marked "amended 2026-10-31, #49":

- §2 rule 6: `ensure_month_partitions(table, from_month, to_month)`, bounded inside the function
  (oldest backfill month to now + 3 months), called by the scheduler and the backfill module.
- §3, §5: the webhook-secret function is owned by the `NOLOGIN` role `ownvitals_webhook_secrets`
  instead of `ownvitals_owner`, with column-level privileges only; it may also clear
  `rotation_requested_at`, which #22's `rotate` sets.
- §3, §4, §5, §7: `ownvitals_maint` can't write `withings_apps` and reads only its non-secret
  columns (guard 11, from #21); PLAN §5's "admin-only" means admins view, the operator changes;
  the client-secret rewrap is an operator step under the owner role.
