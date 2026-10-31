# Tenant isolation: contexts, sessions, roles and guards

How ownvitals keeps one user's health data away from every other user: [ADR 0002](../adr/0002-tenant-isolation.md)
(PLAN §5). Two walls: every repository filters `owner_id = ctx.user_id` itself, and every owned
table has **forced** row-level security keyed on the transaction setting `app.user_id`. Web and
the worker connect as `ownvitals_app`, which can't bypass either.

## The pieces

| Path | What |
|---|---|
| `backend/src/ownvitals/context.py` | `UserContext(user_id, role, connections)`: immutable, hashable, built only with the issuer token (`issuer_token()`), never from request data (pydantic refuses it), never pickled; `require_context` accepts the exact class only |
| `backend/src/ownvitals/db/session.py` | `user_session(factory, ctx)`: one transaction with `app.user_id` set transaction-locally; `system_session(factory)`: one transaction with no user (system tables only; owned tables show no rows). Both own their transaction: `commit()`/`rollback()` inside raise `TransactionOwnedError` |
| `backend/src/ownvitals/db/rls.py` | `enable_owner_rls(table)` for migrations: `ENABLE` + `FORCE ROW LEVEL SECURITY` and the `owner_isolation` policy |
| `backend/src/ownvitals/db/tables.py` | `SYSTEM_TABLES` (table → reason; no `owner_id`, no RLS) and `NO_APP_ACCESS` |
| `backend/src/ownvitals/db/role_check.py` | `verify_database_role` (startup) and the per-connection and per-checkout checks `create_engine` installs |
| `docker/postgres/init/10-roles.sql` | the roles, grants and defaults of ADR 0002 §5; idempotent; run as the superuser |
| `backend/migrations/versions/0002_close_alembic_version.py` | closes Alembic's table to the app role |

## Roles

| Role | Used by | Notes |
|---|---|---|
| `postgres` | the db init script only (and, from #9, `db-functions`) | never web, worker or migrate |
| `ownvitals_owner` | `migrate` (Alembic) | owns the database, schema `public` and every table; no `BYPASSRLS`; its password never reaches web or worker |
| `ownvitals_app` | web, worker | `NOINHERIT`, member of no role, owns nothing; `CONNECT`, `USAGE` on `public`, DML on tables and `USAGE, SELECT` on sequences through the owner's default privileges; never `TRUNCATE`, `REFERENCES`, `TRIGGER`, `MAINTAIN`, `CREATE` or `TEMP`; role defaults `statement_timeout 30s`, `lock_timeout 5s`, `idle_in_transaction_session_timeout 60s` |

`10-roles.sql` reads the two passwords from `$DATABASE_OWNER_PASSWORD_FILE` (default
`/run/secrets/database_owner_password`) and `$DATABASE_PASSWORD_FILE` (default
`/run/secrets/database_password`), the same bytes `config.py` reads (one trailing LF, then one CR,
stripped). It refuses an empty password file and a non-superuser. Running it again repairs what
it manages (attributes, passwords, memberships, role settings, grants, default privileges).

In compose, `db` runs it on the **first start of an empty volume** (`docker-entrypoint-initdb.d`);
an older volume needs `docker compose down -v`. `migrate` connects as `ownvitals_owner` with the
`database_owner_password` secret, `web` as `ownvitals_app` with `database_password`. CI runs it
twice on the empty database (the `backend` job's roles step).

**Web and the worker refuse to start** (`DatabaseRoleError`) when their role:

- is a **member of any role**, directly or indirectly, with or without `INHERIT` or `SET`
  (`pg_has_role(..., 'MEMBER')`). A member can use the role's rights, through `INHERIT` or by
  `SET ROLE`: a member of `ownvitals_owner` can disable the tables' row security, a member of
  `pg_read_all_data` ignores every grant, and the same goes for a superuser or `BYPASSRLS` role
  and `pg_execute_server_program`, `pg_read_server_files` and `pg_write_server_files`. No
  membership is allowed (the roles script grants none);
- is a superuser, or has `BYPASSRLS` or `REPLICATION`;
- owns a table, a schema or the database;
- has role settings other than the three timeouts;
- meets a default for any `app.*` setting (`app.user_id` is the RLS user): on the role itself,
  on the database (`ALTER DATABASE ... SET`) or on every role (`ALTER ROLE ALL [IN DATABASE ...]
  SET`).

The attribute and ownership checks also cover every role the role is a member of, so a report
names what a membership would give. The engine repeats the check on every new connection, and on
every pool checkout checks that the connection carries no `app.user_id` and has exactly the
role's timeouts (otherwise the pool replaces it). A refused login is fatal too; an unreachable
database is not (the per-connection check still applies). There is no override, not even in
`dev`. Guard 2 (`role_guard.py`) checks the same on the test database, and refuses an `app.*`
default for any role or database.

## The test database

The database tests need the roles in the throwaway Postgres of [backend.md](backend.md). The
fixtures (`role_urls`, `app_database_url`, `owner_database_url`) replace both passwords with
random ones per run; without the roles the tests skip locally and fail in CI. With `psql`
installed:

```bash
mkdir -p /tmp/ov-secrets
head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' > /tmp/ov-secrets/owner
head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' > /tmp/ov-secrets/app
DATABASE_OWNER_PASSWORD_FILE=/tmp/ov-secrets/owner DATABASE_PASSWORD_FILE=/tmp/ov-secrets/app \
  PGPASSWORD=test psql -h 127.0.0.1 -p 30002 -U postgres -d ownvitals -v ON_ERROR_STOP=1 \
  -f docker/postgres/init/10-roles.sql
```

Without a local `psql`, run it inside the Postgres container (here issue 2's `ownvitals-2-pg` of
[backend.md](backend.md)):

```bash
PG=ownvitals-2-pg
MSYS_NO_PATHCONV=1 docker exec "$PG" sh -c 'mkdir -p /tmp/s && for f in owner app; do
  head -c 16 /dev/urandom | od -An -tx1 | tr -d " \n" > /tmp/s/$f; done'
docker cp docker/postgres/init/10-roles.sql "$PG":/tmp/10-roles.sql
MSYS_NO_PATHCONV=1 docker exec -e DATABASE_OWNER_PASSWORD_FILE=/tmp/s/owner \
  -e DATABASE_PASSWORD_FILE=/tmp/s/app "$PG" \
  psql -X -q -v ON_ERROR_STOP=1 -U postgres -d ownvitals -f /tmp/10-roles.sql
```

`test_roles_script.py` and `test_roles_rerun.py` call `psql` themselves: they skip without it
locally and fail without it in CI.

## Adding tables

**An owned table** (any per-user data) in its migration:

- `id uuid PRIMARY KEY DEFAULT gen_random_uuid()`, `owner_id uuid NOT NULL REFERENCES users (id)
  ON DELETE CASCADE` (once `users` exists, #10), `UNIQUE (owner_id, id)`;
- every other unique constraint or index includes `owner_id` (the one exception, #22's
  `connections (app_id, withings_userid)`, is registered by name in `rls_guard.GLOBAL_UNIQUE_INDEXES`);
- foreign keys to owned tables include the owner: `FOREIGN KEY (owner_id, x_id) REFERENCES
  x (owner_id, id)`; `ON DELETE` is `CASCADE`, `RESTRICT`/`NO ACTION` or `SET NULL (<col>)`;
- then `enable_owner_rls("<table>")`. Further policies are `AS RESTRICTIVE`
  (`rls_guard.REQUIRED_DENY_POLICIES` / `REQUIRED_POLICIES` pin the ones a table must have).

Partitioned tables are refused until #21 adds the partition rules (roadmap Amendment 2).

**A system table** (no `owner_id`, read before a user is known): add it to `SYSTEM_TABLES` with a
one-line reason, and to `NO_APP_ACCESS` if the app role must not touch it. A migration that
grants the app role less than full DML also adds the table to
`role_guard.RESTRICTED_APP_PRIVILEGES` and its restore lines to `10-roles.sql` (after the blanket
re-grant), so a re-run of the script keeps the narrower grants (`test_roles_rerun.py`).

## Who may open a session

`tests/session_scan.py` keeps two allowlists (packages and exact modules):

| Session | Allowed |
|---|---|
| `user_session` | `ownvitals.db`, `ownvitals.auth`, `ownvitals.connections` |
| `system_session` | `ownvitals.db`, `ownvitals.auth` |

Everything else, worker and job-kind code included, fails `test_session_guard.py`: any reference
to the names (and to `_owned_session` and `SET_USER`, which only `ownvitals.db` may name), an
import of `ownvitals.db.session` or anything in it, a name or attribute chain that reaches that
module (`ownvitals.db.session.X`, `db.session.X` after `from ownvitals import db`, `m.X` after
`import ownvitals.db.session as m`), or a string that sets `app.user_id`. Relative imports are
resolved to absolute names first (`from ..db import session` in `ownvitals.jobs.sync` is caught),
and one above the top-level package is refused. Worker writes go through the fenced helper of #9. A package joins a list in its own PR, with the reason
in `session_scan.py`'s docstring and the pinning test updated (#8 KEK check, #9 jobs, #12 flags
and admin are expected).

Only `ownvitals/context.py`, `ownvitals/auth/deps.py` (#10) and `ownvitals/jobs/issuer.py` (#9)
may build a `UserContext` (`user_context_scan.py`); handlers get it through the auth dependency,
never from a path, query, body or header. The same scan refuses, in every module:

- `dataclasses.replace`, `object.__setattr__`, `object.__new__`, `object.__delattr__` (also taken
  as a value), `x.__class__(...)` and `type(x)(...)` (ADR 0002 §1: "anywhere");
- assigning `__class__` (`x.__class__ = K`, `del x.__class__`) and the strings `"__class__"`,
  `"__init__"` and `"__post_init__"`, so `setattr(x, "__class__", K)`,
  `getattr(K, "__post_init__")` and `vars(K)["__init__"]` fail;
- naming `__init__` or `__post_init__` at all (a call, `hook = K.__post_init__`, a class body
  naming it), except a zero-argument `super().__init__(...)` or `super().__post_init__(...)`
  written directly in a method of the same name. Called on another class, either runs that
  class's code with a real context as `self`, and a frozen dataclass's generated `__init__` with
  the same field names rewrites the context in place.

The guard reads "anywhere" with one exception (Architect, 2026-10-31): the frozen-dataclass idiom
`object.__setattr__(self, "field", value)` written directly in a class's undecorated
`__post_init__(self, ...)`, with the bare `self` first, a string literal that does not start with
`__` second, and `self` never rebound there. With the rules above only the dataclass's own
`__init__` calls that method, so `self` is an instance of that class. Another target, another
method, a computed or dunder field name, a nested function or lambda, or an alias stays refused.

What the scan catches is accidental misuse and the obvious forgeries, as listed. It does not
stop deliberate obfuscation (a name built at run time such as `getattr(K, "__post_" + "init__")`,
`inspect`, `importlib`): ADR 0002 §1 leaves deliberately malicious code inside the process to the
review gate.

## Guards (ADR 0002 §7)

| # | What | Test |
|---|---|---|
| 1 | every table in `public` is a listed system table or meets the owned-table contract | `test_rls_guard.py` (`rls_guard.check_tables`) |
| 2 | roles and every privilege as an allowlist | `test_role_guard.py` (`role_guard.check_roles`), `test_roles_rerun.py` |
| 3 | two users on test-only owned tables: no cross-user read, write, move, child reference or unique-value probe; no rows without a context; the app role can't change the schema | `test_isolation.py` |
| 4 | no `app.user_id` or held cursor left on a pooled connection; `commit()` inside a session raises | `test_isolation.py` |
| 5 | `UserContext` only from its issuers | `test_user_context_guard.py`, `test_context.py` |
| 6 | startup refusal of a privileged role | `test_role_check.py` |
| — | only allowlisted modules open a session | `test_session_guard.py` |

Each guard has tests that plant a violation and show the guard catches it.

## Execution contexts (ADR 0002 §4)

`tests/ctx.py` runs the same owned-data operations (`add`, `names`, `find`, `delete` on the
test-only table `t_parent`) the way each execution context reaches owned rows:

- `web_request`: the real app from `create_app` (lifespan and startup role check included) plus a
  test-only router whose dependency stands in for the auth issuer (an opaque session token picks
  the user);
- `worker_job`: its own engine, checked at start, and a context issued from a synthetic job row.

`test_two_user_isolation.py` runs two users at once in each context and in a mix, and verifies
nothing crosses; it also shows RLS alone holds when the repository's filter is removed, and that
the check fails when both walls are gone. `test_owner_filter.py` shows the repository's own filter
holds with RLS bypassed (`bypass_url`, a superuser engine). #15 adds the scheduler, webhook and
admin contexts to `EXECUTION_CONTEXTS`; later repositories reuse `bypass_url` to prove their own
filters.
