# Jobs: the queue, leases, the fence and the worker

How ownvitals runs background work: [ADR 0005](../adr/0005-jobs-and-leases.md) (PLAN §4.3,
§4.4). One queue table in Postgres, claimed with `FOR UPDATE SKIP LOCKED` through three reviewed
`SECURITY DEFINER` functions; a lease per run with a fencing token (`lease_version`) that every
worker write checks first; `LISTEN/NOTIFY` wake-ups; a worker process; a jobs API and an SSE
progress stream. #9 ships the machinery with **no kind** registered: M1 adds the Withings kinds.

## The pieces

| Path | What |
|---|---|
| `backend/migrations/versions/0003_jobs.py` | `jobs` and `job_events` (owned, forced RLS; events append-only with restrictive `USING (false)` policies for `UPDATE` and `DELETE`; owner-inclusive key to their job, `ON DELETE CASCADE`) |
| `docker/postgres/queue/20-queue.sql` | the role `ownvitals_queue` and the functions `queue_claim`, `queue_reap`, `queue_stats`; idempotent, run as the superuser |
| `docker/postgres/queue/db-functions.sh` | the entrypoint of compose's `db-functions` one-shot |
| `backend/src/ownvitals/db/queue_check.py` | the startup and per-connection queue check (`install_queue_check`, `verify_queue`, `queue_verified`) |
| `backend/src/ownvitals/db/queue_digests.py` | the expected `md5(pg_get_functiondef)` of each function (generated) |
| `backend/src/ownvitals/db/queue_locks.py` | the advisory-lock namespaces of ADR 0005 §9 |
| `backend/src/ownvitals/jobs/` | kinds, the issuer, the fence, transitions, runs, the runner, the user's transitions, the API, the event hub and stream |
| `backend/src/ownvitals/worker/` | the loop, its `LISTEN`, and `python -m ownvitals.worker` |
| `backend/tests/queue_guard.py` | guard 9: a second implementation of the queue check, read as the superuser |

## Roles and functions

`ownvitals_queue` is `NOLOGIN NOINHERIT BYPASSRLS`, member of no role, with exactly `USAGE` on
`public`, `SELECT, UPDATE` on `jobs` and `SELECT, INSERT` on `job_events`. It owns the three
functions and nothing else. Each function is `SECURITY DEFINER` with `search_path = pg_catalog,
pg_temp`, the comment `ownvitals-queue v1`, and `EXECUTE` for `ownvitals_app` and its owner only.

| Function | Does | Returns |
|---|---|---|
| `queue_claim(worker, lease_seconds, kinds)` | claims the next claimable job of one of `kinds` (`SKIP LOCKED`), bumps `attempt` and `lease_version`, writes the `claimed` event | `(job_id, owner_id, lease_version)` or nothing |
| `queue_reap(limit)` | requeues (with backoff), fails (`lease_lost`) or cancels expired leases, bumping the token; fails queued jobs whose attempts are spent (`internal`) | the count |
| `queue_stats()` | counts per state, across users | `(metric, total)`: no ids |

No user code may run as `ownvitals_queue`: the script, the queue check and guard 9 all refuse
any other user-defined function, operator, trigger or rule, and any default, check, index or
partition expression that calls a built-in running SQL text (`query_to_xml` and family,
`ts_stat`, `ts_rewrite`, `set_config`). The database code later issues add is admitted there
exactly, pinned by digest, in the PR that adds it.

### db-functions

Compose runs `db-functions` (`restart: "no"`) after `migrate`; `web` and `worker` wait for it to
complete. It runs `20-queue.sql` as the superuser in one transaction on every start: the script
is idempotent and repairs what it manages (attributes, memberships, settings, privileges,
overloads, owner, ACL, comment). What it must not repair (an object the queue role owns besides
the functions, default privileges, foreign user code) stops it with a message naming the object.

It runs the **application image**, which carries `20-queue.sql` and the entrypoint
`db-functions.sh` under `/app/db-functions` (`Dockerfile`; root-owned, mode 0555). Nothing is
mounted from the host, so the functions installed are always those of the image's release: on
the server the deploy pins `db-functions` to the release digest with the other app services, and
a rollback re-installs the previous release's functions ([deploy.md](deploy.md), "Database
functions"). The entrypoint writes the `postgres_password` secret (it is the only service besides
`db` holding it) to a `PGPASSFILE` on a tmpfs, escaping `\` and then `:` as libpq's `.pgpass`
format requires, and refuses an empty secret or one with a line break. Then
`python -m ownvitals.db.install_functions` (`backend/src/ownvitals/db/install_functions.py`)
sends the SQL to `db` in one transaction with psycopg: no psql in the image. The runner refuses a
connection string with a password or anything but host, port, database, user, `connect_timeout`
and `sslmode`, every `PG*` environment variable but `PGPASSFILE` (`PGPASSWORD`, `PGSERVICE`,
`PGOPTIONS`, ..., which libpq would read), and a missing `PGPASSFILE`; the password never
reaches the environment, a command line, the output or `docker inspect`. It also refuses, before
connecting, SQL that controls its own transaction (a top-level `BEGIN`, `START`, `COMMIT`, `END`,
`ROLLBACK`, `ABORT`, `SAVEPOINT`, `RELEASE` or `PREPARE`; the scan skips comments, literals and
dollar-quoted bodies, so PL/pgSQL's `BEGIN ... END` passes): such a file could commit part of the
work before a later error.

Tests: `test_compose_roles.py` pins the service (image, entrypoint, the tmpfs as its only mount,
no volume, environment or user) and checks that no service reads SQL from a host path but `db`'s
first-start init directory ([stack.md](stack.md)); `test_db_functions_script.py` runs the
entrypoint on passwords with `\` and `:` and checks the line against libpq's `.pgpass` rules,
signs in through libpq with it, and runs the runner (one transaction, rolled back on an error);
`test_install_functions_sql.py` covers the transaction-control scan. The host-SQL check parses the
compose YAML: short entries (plain, quoted, block or flow lists) and long ones (`type: bind`).
The deploy refuses a production `db-functions` with any volume ([deploy.md](deploy.md), check 11).

### The Postgres image

`db` runs `postgres:17.11@sha256:31ba9c94a03ada9988f2bb493a22a75eef3a185884533e5cdbcd498ea835b08f`,
and CI's `backend` service the same reference: the expected function digests depend on how that
image renders the functions, and `test_compose_roles.py` fails when the two differ. The digest is
the tag's **multi-arch image index** on Docker Hub (the registry's index, not a platform
manifest), read with `docker buildx imagetools inspect postgres:17.11` (the `Digest:` line).
Dependabot's `docker-compose` entry bumps the tag and digest in `compose.yaml`; that PR must set
the same reference in `ci.yml` (Dependabot does not see a service image there) and regenerate
`queue_digests.py` if the rendering changed. On the server, pull the database image by that
digest before a deploy (the deploy runs compose with `--pull never`).

### Changing a function

1. Edit `20-queue.sql`, keep it idempotent, and bump the version comment if the contract changes.
2. Against a database at head with the script run (the test superuser, the pinned `postgres:17.11`):
   `uv run python tests/queue_digests.py --write`.
3. CI's `backend` job migrates, runs the script twice and runs `--check`: a stale
   `queue_digests.py` fails there.

## The queue check

The worker and `python -m ownvitals jobs stats` refuse to run on a drifted queue
(`QueueDriftError`, a `DatabaseRoleError`): at startup (`verify_queue`) and on **every new pooled
connection** (`install_queue_check`, a `connect` listener after the role check). The worker
claims nothing until a connection has passed (`queue_verified`); a database that is down at
startup is checked on its first connection. Drift anywhere (the background check, a claim, a
run's connection, a heartbeat, maintenance) stops the worker, never a job's attempt; running
jobs are handed back first. The check names objects, never data or credentials.

**Gap against ADR 0005 §3 (#77 item 3):** web does not run the queue check yet. Its lifespan
runs only the event hub and the stream limiter; the web refusal of a drifted queue comes with
#77.

## Leases and the fence

A claim returns only `(job_id, owner_id, lease_version)`. The issuer (`jobs/issuer.py`) asks the
account directory whether the owner is active and for their connection refs, then builds a
`UserContext` with `role="user"`. Every worker write is a `fenced_session(factory, lease)`: a
`user_session` as the owner whose first statement is

```sql
UPDATE jobs SET updated_at = now()
 WHERE id = :job_id AND owner_id = :owner_id AND lease_version = :v AND state = 'running'
RETURNING cancel_requested_at, next_seq
```

0 rows raises `LeaseLost` and rolls back. Each transition statement carries the same conditions
itself. The fence's row lock lasts to the commit, so the reaper (which skips locked rows) can't
bump the token in between. **Never nest** fenced transactions (the inner fence waits for the
outer one's lock until `lock_timeout`).

A kind's handler gets a `JobRun`: `checkpoint()`, `progress(done, total)`, `fenced()` for its own
writes and `provider_call(deadline)` around a provider call (the lease is extended to the call's
deadline plus 30 s and committed before the call; no transaction is open across it). It ends with
`Complete(store)`; `store` runs in the final fenced transaction. It fails a run by raising
`JobFailure(category)`; only the category is ever kept (`jobs/errors.py`, `jobs/sentences.py`).
The heartbeat extends the lease every lease/3 with `GREATEST`.

## The worker

`python -m ownvitals.worker` (compose `worker`): up to `worker_concurrency` runs, woken by `LISTEN
jobs_queued` or a poll every `worker_poll_seconds`; the reaper every `worker_maintenance_seconds`.
On SIGTERM it stops claiming, runs stop at their next checkpoint and hand their leases back
(`attempt - 1`, `lease_version + 1`, event `requeued`; `cancelled` if a cancel was requested);
runs still busy after `worker_shutdown_wait_seconds` are cancelled and handed back too. Compose
gives 30 s. Exit code 2: a privileged role or a drifted queue.

## The API and the stream

`POST /api/jobs` (`Idempotency-Key` header, `{kind, input}`), `GET /api/jobs/{id}`,
`POST /api/jobs/{id}/cancel`, `GET /api/jobs/{id}/events` (SSE, `Last-Event-ID`, keepalive 15 s,
at most 10 minutes and 5 streams per user and process, 204 for an ended job with nothing new).
Another user's job is the same 404 as an unknown or malformed id. Every event payload is
validated against its model when written and again when sent (`jobs/events.py`).

`python -m ownvitals jobs stats` prints `<metric> <count>` per state.

## Seams until #10, #13 and #77 (fail closed)

| Seam | Default in #9 | Replaced by |
|---|---|---|
| `jobs.owner_id` foreign key | none (no `users` table yet) | #10 adds it, `ON DELETE CASCADE` |
| account directory (`jobs/issuer.py`, `NO_ACCOUNTS`) | every owner inactive: every job fails `account_disabled`, no connection refs | #10 (`ownvitals.auth`) and #13 (`ownvitals.connections`) |
| `current_user` (`jobs/deps.py`) | every jobs route answers 401 `not_signed_in` | #10 (full session, CSRF) |
| `stream_session_probe` | the session is never valid: a stream ends at its first check | #10 |
| `KINDS` | empty: the worker claims nothing, the API refuses every kind | M1 |
| worker start-up | no `egress.install_worker_allowlist` (#6), no `crypto.keks.load_keyring` (#8) | #77 item 1 |

Each default is pinned by a test (`test_jobs_fence.py`, `test_jobs_api.py`, `test_jobs_stream.py`,
`test_jobs_schema.py`, `test_jobs_errors.py`).

## Guard 2 and the queue (#77 item 2)

Guard 2 (`tests/role_guard.py`) does not know `ownvitals_queue` or the app role's `EXECUTE` on the
queue functions yet. Until #77 teaches it, **guard 9 is the only guard that covers them**, and
nothing leaves the queue installed for guard 2 to see: the test fixtures (`tests/jobtools.py`)
install it per module and uninstall it afterwards (`DROP OWNED BY ownvitals_queue CASCADE`, `DROP
ROLE`), and CI's queue step does the same before the tests.

## Tests

Against the throwaway Postgres (`OWNVITALS_TEST_DATABASE_URL`, see [backend.md](backend.md)),
synthetic owners (random UUIDs) and the test-only kind `test_kind` only:

| File | Covers |
|---|---|
| `test_queue_functions.py` | the three functions as `ownvitals_app`; concurrent claims |
| `test_queue_guard.py` | guard 9, the startup and per-connection checks, drift, the script's repairs and refusals, `jobs stats` |
| `test_jobs_fence.py` | the issuer, the fence, the heartbeat, a stale holder after the reaper (acceptance 1) |
| `test_jobs_worker.py` | two workers never run one job at once (acceptance 2), the loop, shutdown, drift |
| `test_jobs_schema.py` | the tables, guard 1, isolation in the worker's context (acceptance 4) |
| `test_jobs_runner.py`, `test_jobs_cancel.py` | runs end to end, retries, cancel |
| `test_jobs_api.py`, `test_jobs_stream.py`, `test_jobs_events.py` | the API, SSE and the hub |
| `test_jobs_errors.py`, `test_worker.py` | categories and backoff; the worker process |
