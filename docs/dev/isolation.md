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

**Web and the worker refuse to start** (`DatabaseRoleError`) when the role they log in as (the
user of the URL, `session_user`):

- runs as another role (`current_user` is not `session_user`), through a default `role` setting
  (`ALTER ROLE ... SET role`) or a connection option (`options=-c role=...`). A login role can
  always go back to itself (`SET ROLE NONE`) or on to any role it may `SET`, so a superuser, or a
  member of `ownvitals_owner`, that starts as `ownvitals_app` could bypass RLS at any time. Every
  check below runs on the login role, never only on the role in effect;
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
  SET`);
- starts its sessions with `app.user_id` set where the catalog doesn't show it: server-wide
  (`ALTER SYSTEM`, `postgresql.conf`) or in the connection options (`PGOPTIONS`,
  `options=-c app.user_id=...`). The check reads the session's live value, so this is a
  `DatabaseRoleError` at startup, not a connection the checkout check keeps replacing.

The attribute and ownership checks also cover every role the role is a member of, so a report
names what a membership would give. The engine repeats the check on every new connection, and on
every pool checkout checks that the connection carries no `app.user_id` and has exactly the
role's timeouts (otherwise the pool replaces it).

A login the database refuses is fatal too (`DatabaseRoleError`, #89): SQLSTATE class 28, 42501
or 3D000. psycopg reports no SQLSTATE at connect, so `role_check.REFUSED_LOGIN` matches the
server's message: a wrong password or role, a role that may not log in, no `pg_hba.conf` entry,
no CONNECT privilege, a connection option the role may not set (`-c session_authorization=...`
or `-c role=...` in `options` or `PGOPTIONS`), or a database that does not exist. The queue check
and the key ring use the same classification. A real connection failure (refused, a timeout,
DNS, a server that is starting up or full) is not fatal: the per-connection check still applies
to every later connection, and no connection is used before it passes.

**The server must report its errors in English** (`lc_messages` `en_US` or `C`, the Postgres
default and the official image's) for these refusals to be classified. In another language a
refusal is read as "unreachable": startup goes on, `/api/ready` answers 503 and every connection
is still refused, so it fails closed but without the clear error. When startup defers on a
`FATAL:` answer it doesn't recognise, the role check logs one warning with the SQLSTATE class
(or `unknown`) and never the message text. A non-superuser can't change `lc_messages` through
the connection options.

There is no override, not even in `dev`. Guard 2 (`role_guard.py`) checks the same on the test
database, and refuses an `app.*` default for any role or database.

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

| Session | Packages | Exact modules |
|---|---|---|
| `user_session` | `ownvitals.db`, `ownvitals.auth`, `ownvitals.connections` | `ownvitals.jobs.fence` (every write a worker makes for a job), `ownvitals.jobs.service` and `ownvitals.jobs.progress` (the jobs API's own writes and reads for the signed-in user, web only) |
| `system_session` | `ownvitals.db`, `ownvitals.auth` | `ownvitals.jobs.queue` (the calls of the queue's `SECURITY DEFINER` functions), `ownvitals.crypto.keks` (the key ring's startup check) |

Everything else fails `test_session_guard.py`: the rest of `ownvitals.jobs` (the runner, the job
kinds, the issuer), all of `ownvitals.worker`, and every other package. Worker code writes through
`ownvitals.jobs.fence`. What fails is any reference to the names (and to `_owned_session` and
`SET_USER`, which only `ownvitals.db` may name), an import of `ownvitals.db.session` or anything
in it, a name or attribute chain that reaches that module (`ownvitals.db.session.X`,
`db.session.X` after `from ownvitals import db`, `m.X` after `import ownvitals.db.session as m`),
or a string that sets `app.user_id`. Relative imports are resolved to absolute names first
(`from ..db import session` in `ownvitals.jobs.sync` is caught), and one above the top-level
package is refused. A package or module joins a list in its own PR, with the reason in
`session_scan.py`'s docstring and the pinning test updated (#12 flags and admin are expected).

**Threat model of guard 5 (Architect, #89).** The scan stops the plausible, review-visible ways
of forging a `UserContext`: mistakes, shortcuts and copied snippets. It is not a sandbox against
adversarial code in the process: a static scan can't prove that Python code running in the same
process can't reach the token (reflection, `gc` and `ctypes` always can). Adversarial code is
covered by code review of every PR, by RLS (the database enforces `owner_id` whatever context
Python builds) and by the role checks. A further route of the same kind (reflection reaching the
token in process) goes under "Known limits" below, not into a new rule, unless it is a plain
import or a call that code review could miss.

Only `ownvitals/context.py`, `ownvitals/auth/deps.py` (#10) and `ownvitals/jobs/issuer.py` (#9)
may build a `UserContext` (`user_context_scan.py`); handlers get it through the auth dependency,
never from a path, query, body or header. The same scan refuses, in every module:

- `dataclasses.replace`, `object.__setattr__`, `object.__new__`, `object.__delattr__` (also taken
  as a value), `x.__class__(...)` and `type(x)(...)` (ADR 0002 §1: "anywhere");
- assigning `__class__` (`x.__class__ = K`, `del x.__class__`) and the strings `"__class__"`,
  `"__init__"`, `"__post_init__"`, `"__new__"`, `"__setattr__"`, `"__delattr__"`, `"__set__"`
  and `"__delete__"`, so `setattr(x, "__class__", K)`, `getattr(K, "__post_init__")`,
  `vars(K)["__init__"]` and `getattr(d, "__set__")` fail;
- naming `__init__`, `__post_init__` or `__new__` at all (a call, `hook = K.__post_init__`, a
  class body naming it), except a zero-argument `super().__init__(...)`,
  `super().__post_init__(...)` or `super().__new__(...)` written directly in a method of the
  same name. Called on another class, a hook runs that class's code with a real context as
  `self`, and a frozen dataclass's generated `__init__` with the same field names rewrites the
  context in place; `type(ctx).__new__(type(ctx))` makes a blank context without the token;
- naming `__setattr__`, `__delattr__`, `__set__` or `__delete__` at all, with no exception:
  `super(type(ctx), ctx).__setattr__` (`super` with or without arguments) and
  `type(ctx).__mro__[-1].__setattr__` reach `object.__setattr__` past the frozen class's own, and
  a slot descriptor (`type(ctx).role.__set__(ctx, "admin")`, also after `d = K.role`) writes a
  field directly. Defining these methods in a class stays allowed;
- the obvious routes to the issuer token (#89, `user_context_rules.py`). A function's globals and
  the module's namespace are the same dict, and it holds the token: with it, `K = type(ctx);
  K(uid, "admin", issuer=tok)` builds an admin context. So the scan refuses:
  - `__globals__`, `__module__`, `__kwdefaults__` and `__defaults__` as an attribute, a name or
    a string (`require_context.__globals__`, `sys.modules[f.__module__]`,
    `K.__init__.__kwdefaults__["issuer"] = tok`);
  - every import that binds the module object `ownvitals.context` (`import ownvitals.context as
    c`, `from ownvitals import context`, `from .. import context`), `ownvitals.context` as an
    attribute of the package outside a type annotation, and the strings `"ownvitals.context"`
    and `".context"` (`sys.modules[...]`, `sys.modules.get(...)`, `importlib.import_module`,
    `__import__`). Names imported from the module (`from ownvitals.context import
    require_context`) stay allowed, and so does a plain `import ownvitals.context` for an
    annotation: it binds only the package, as `import ownvitals.db` does, and each use is judged.
    A star import from `ownvitals` or any of its packages, relative ones included, is refused:
    `ownvitals` has no `__all__`, so `from ownvitals import *` binds the module `context`;
  - an `issuer=` keyword in any call, and a `**` argument that spells `"issuer"` anywhere
    (`**{"issuer": t}`, `**dict(issuer=t)`, `**{k: t for k in ["issuer"]}`,
    `**dict([("issuer", t)])`, `**({} | {"issuer": t})`). Only the issuer modules pass the token,
    and they are not scanned. A third-party API with a keyword of that name (a JWT library's
    `issuer=`, say) needs the Architect;
- `copyreg` as an import, a name, an attribute or a string (`importlib.import_module("copyreg")`):
  `copyreg.__newobj__(type(ctx))` and `copyreg._reconstructor` make a blank context without the
  token (#89);
- `eval`, `exec` and `compile`, the builtins (#89): as a name, imported from `builtins` under
  any alias, or as an attribute of `builtins` or `__builtins__`. Code in a string runs unseen.
  `re.compile`, `ast.literal_eval` and other objects' methods of those names stay allowed.

**Annotations follow an allow-list type grammar** (#89, Architect's decision on #115,
`tests/user_context_types.py`). Annotations run on Python 3.12: parameter, return and variable
annotations when the `def` or the statement runs, and every string in one when
`typing.get_type_hints` resolves it. So every annotation position (parameters, returns,
variable annotations, the value of `X: TypeAlias = ...`, PEP 695 `type` aliases and
type-parameter bounds) may hold only a type expression:

```
type  := name | name.name...          (no part starting with _, never the token's names)
       | name[ type-args ]            (a subscript of a dotted name)
       | type | type                  (X | Y)
       | *type                        (*Ts)
       | constant                     (None, ..., numbers)
       | "string"                     (parsed and checked as a type, in its place, recursively)
type-args := type | (type, ...) | [type, ...]
```

- `Literal[...]`'s arguments are values: constants, dotted names (`Color.RED`) and nested
  `Literal`s. They are never parsed (`Literal["a", "b"]` passes).
- In `Annotated[T, m1, m2]`, `T` is a type and the metadata `m1, m2` is ordinary code, the one
  place in an annotation where a call is allowed (FastAPI's `Depends(get)`, `Header()`,
  `Field(ge=1)`). Every rule in this section applies to the metadata as it does outside an
  annotation, so `getattr(object, "__setattr__")(...)`, `vars(ownvitals.context)` and
  `UserContext(...)` are refused there. A string in the metadata is a value, not parsed.
- `Literal` and `Annotated` count only when imported from `typing` or `typing_extensions`,
  under any alias, and not rebound in the module (an assignment, a `def` or `class`, another
  import, or the module alias rebound: `import typing as t; t = dict`). Anything else named so
  is a plain generic, where a call is refused.
- A string in a type position, nested ones included (`list["..."]`, `X | "..."`, a string in a
  string), is parsed **into the module's tree** before any rule runs, so its code is judged with
  the module's own imports and aliases: `"Annotated[int, vars(ownvitals.context)]"` after
  `import ownvitals.context`, an aliased `UserContext` and `dataclasses.replace` are refused as
  they are unquoted. A string that doesn't parse stays a string (`get_type_hints` would raise on
  it); strings nested more than 8 deep are refused.
- Anything else in a type position is refused: a call, an attribute of a call, a subscript of a
  non-name, a lambda, a comprehension, a walrus.

The guard reads "anywhere" with one exception (Architect, 2026-10-31): the frozen-dataclass idiom
`object.__setattr__(self, "field", value)` written directly in a class's undecorated
`__post_init__(self, ...)`, with the bare `self` first, a string literal that does not start with
`__` second, and `self` never rebound there. Code that **names** the method can't call it (the
rules above refuse the name), but code can still reach it without the name: `for f in
vars(A).values(): f(ctx)` runs it with a context as `self`. So the exemption never covers one of
`UserContext`'s own field names (`user_id`, `role`, `connections`; `CONTEXT_FIELDS`, pinned to
the class by `test_user_context_writers.py`), in any method (#89). Another target, another method,
a computed, dunder or context field name, a nested function or lambda, or an alias stays refused.

What the scan catches is accidental misuse and the obvious forgeries, as listed: it reads source
text, so it sees only names written out. It does not stop deliberate code, which ADR 0002 §1
leaves to the review gate. Known limits:

- a name built at run time: `getattr(K, "__post_" + "init__")`,
  `getattr(super(type(ctx), ctx), "__SETATTR__".lower())`, `b"__setattr__".decode()`, or a
  module name built the same way for `importlib` or `sys.modules`;
- the module reached through the package object, literally or not: `getattr(ownvitals,
  "context")`, `vars(ownvitals)["context"]`, `sys.modules["ownvitals"].context`,
  `vars(sys.modules["ownvitals"])`, `getattr(ownvitals, "con" + "text")`. With the module,
  `vars(m)` and `m.__dict__` give the token, and neither can be refused outright: other code
  (`ownvitals/log.py`) uses both;
- the general class, reflection in the process: walking package or module objects, searching
  `sys.modules`, `gc.get_referrers`/`gc.get_objects`, `inspect` (`inspect.getmodule(K)`, a
  function's closure or frame), and `ctypes`;
- a keyword dict bound to a name first: `kw = {"issuer": tok}`, then `K(..., **kw)`;
- code evaluated from a string outside annotations, other than through `eval`, `exec` and
  `compile`: `typing.get_type_hints` on a string built at run time, `pickle`, `pydoc.locate` and
  the like are reflection under the threat model;
- class-level patching: `type(ctx).role = property(...)` changes what every context reads
  (accepted by the Architect);
- a hook reached without its name, as above, when it sets a field that isn't one of the context's:
  harmless to a context, which has only those slots.

## Guards (ADR 0002 §7)

| # | What | Test |
|---|---|---|
| 1 | every table in `public` is a listed system table or meets the owned-table contract | `test_rls_guard.py` (`rls_guard.check_tables`) |
| 2 | roles and every privilege as an allowlist | `test_role_guard.py` (`role_guard.check_roles`), `test_roles_rerun.py` |
| 3 | two users on test-only owned tables: no cross-user read, write, move, child reference or unique-value probe; no rows without a context; the app role can't change the schema | `test_isolation.py` |
| 4 | no `app.user_id` or held cursor left on a pooled connection; `commit()` inside a session raises | `test_isolation.py` |
| 5 | `UserContext` only from its issuers | `test_user_context_guard.py`, `test_user_context_writers.py`, `test_user_context_routes.py`, `test_user_context_types.py`, `test_context.py` |
| 6 | startup refusal of a privileged role, and of a login the database refuses | `test_role_check.py`, `test_role_check_login.py`, `test_role_check_refused.py` |
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
