# Backend: commands, settings and layout

The backend is a uv project in `backend/` (Python 3.12, package `ownvitals` in `src/ownvitals`).
Run everything from `backend/`. CI (`backend` job) runs exactly the commands below, with uv 0.13.6.

## Commands

The globally installed uv may be older; use the version CI uses and never let another one write
`uv.lock`:

```bash
cd backend
uvx uv@0.13.6 sync --locked          # install from uv.lock; fails if the lock is stale
uvx uv@0.13.6 lock                   # after changing pyproject.toml (the only command that writes uv.lock)
uvx uv@0.13.6 run ruff check
uvx uv@0.13.6 run ruff format --check
uvx uv@0.13.6 run pyright
uvx uv@0.13.6 run pytest -q                       # needs the test database, see below
uvx uv@0.13.6 run pytest -q tests/test_config.py  # one file
uvx uv@0.13.6 run python -m ownvitals --help      # serve the API (--host, --port)
```

Ruff (`line-length = 100`, rules `E F W I UP B SIM ASYNC RUF S PT`), pyright (`standard`) and
pytest (`asyncio_mode = auto`, `filterwarnings = ["error"]`) are configured in `pyproject.toml`.
Keep files under 500 lines.

### Dependencies

All runtime dependencies of the platform milestone (M0), plus numpy and scipy for the analysis
and ECG cores, are declared up front: alembic, argon2-cffi, authlib, certifi, cryptography,
fastapi, httpx, joserfc, numpy, psycopg[binary], pydantic, pydantic-settings, pyotp, scipy, segno,
sqlalchemy[asyncio], uvicorn[standard]. Dev: pyright, pytest, pytest-asyncio, ruff. Group `audit`:
pip-audit (CI only). Packages whose modules arrive later are installed already, so the lockfile
does not change under parallel issues; nothing imports numpy or scipy yet. The exact versions are
the ones in `uv.lock`.

## The test database

Database tests need a real Postgres 17. Start a throwaway one per worktree (name and port are
yours; the stack script uses `30000 + issue` for the compose database, so pick another port or
stop the stack):

```bash
MSYS_NO_PATHCONV=1 docker run -d --rm --name ownvitals-2-pg -e POSTGRES_PASSWORD=test \
  -p 127.0.0.1:30002:5432 postgres:17.11
MSYS_NO_PATHCONV=1 docker exec ownvitals-2-pg psql -U postgres -c 'CREATE DATABASE ownvitals'
export OWNVITALS_TEST_DATABASE_URL=postgresql+psycopg://postgres:test@127.0.0.1:30002/ownvitals
# when done
docker stop ownvitals-2-pg
```

Then create the database roles in it with `docker/postgres/init/10-roles.sql` (commands in
[isolation.md](isolation.md)).

`OWNVITALS_TEST_DATABASE_URL` is a superuser URL of the throwaway database, for test setup and
probes only: the code under test connects as `ownvitals_app` (`app_database_url`) or
`ownvitals_owner` (`owner_database_url`), whose passwords the fixtures set per run. Without the
variable or the roles the database tests skip with a reason; with `CI=true` either **fails** the
run (`tests/dbtools.py: unavailable`), so they can never be skipped silently in CI. The URL must
name a host on this machine (loopback, `localhost` or a socket directory, also in `?host=` and
`?hostaddr=`); a URL without a host, with an empty entry in a host list, or with `?service=` is
refused, and the fixtures remove `PGHOST`, `PGHOSTADDR` and `PGSERVICE` from the test process.
libpq connects on its own, outside the network guard below, so it must never pick a host itself.

## No network in tests

`tests/netguard.py` is installed by an autouse fixture: `socket.connect`, `connect_ex` and
`getaddrinfo` raise `NetworkBlocked` before anything is sent unless the target is loopback
(`127.0.0.0/8`, `::1`, `localhost`) or a Unix socket. The throwaway Postgres, in-process fakes and
local servers stay on loopback. A test that needs more must carry `@pytest.mark.allow_network`;
none does, and none should. Fixtures are synthetic; there is no real health data anywhere.

Known limits of the guard, which patches Python's socket layer only: it does not see UDP
(`sendto`), `gethostbyname*`, or connections libpq makes itself (sync psycopg to any host, async
psycopg to an IP literal). So a test must never open such a connection: the database fixture
enforces a loopback URL, and anything else that talks to a network is a fake on loopback.

`conftest.py` also removes every `OWNVITALS_*` variable (except the test database URL) from the
environment and points `OWNVITALS_SECRETS_DIR` at an empty directory, so a developer's shell or
`/run/secrets` never changes a test.

## Settings

`ownvitals.config.Settings` (pydantic-settings) reads `OWNVITALS_<NAME>` environment variables.
**Secrets are read only from files** in `OWNVITALS_SECRETS_DIR` (default `/run/secrets`), named
after the field without the prefix (`/run/secrets/database_password`); an environment variable of
a secret field is ignored. Only a trailing newline is stripped. Anything that can carry a
credential is a `SecretStr`, masked in `repr`, dumps and validation errors. Empty environment
values count as unset.

All settings of the platform milestone exist already, so the issues that use them add no field:

| Setting | Default | Notes |
|---|---|---|
| `environment` | `dev` | `dev`, `test`, `prod`; the **image** sets `prod`, compose sets `dev`. `prod` needs an `https://` `public_base_url`, hides `/api/docs` and refuses a password inside `database_url`, `migrations_database_url` or `maint_database_url` (passwords come from their secret files) and any query parameter outside the allowlist below |
| `log_level` | `INFO` | |
| `database_url` | `postgresql+psycopg://ownvitals_app@127.0.0.1:5432/ownvitals` | app role (web, worker) |
| `migrations_database_url` | `...//ownvitals_owner@127.0.0.1:5432/ownvitals` | owner role, Alembic only |
| `maint_database_url` | unset | admin role; needs `database_maint_password` too |
| `static_dir` | unset | built SPA; unset: API only |
| `public_base_url` | `http://127.0.0.1:8000` | the one public origin, normalised |
| `admin_emails`, `trusted_proxies` | empty | comma-separated; proxies are IPs or CIDR ranges |
| `session_idle_days`, `session_absolute_days` | 7, 30 | |
| `password_hash_workers` | 4 | |
| `signup_mode` | `invite` | `invite`, `closed`, `open` |
| `stepup_window_minutes` | 10 | how long a fresh TOTP check authorises a sensitive action (PLAN §6: 10 minutes; 1 to 10, so the environment can only shorten it) |
| `purge_interval_seconds` | 3600 | |
| `connection_kek_id` | `kek-1` | `kek-[a-z0-9-]{1,28}`; the key is the secret file `connection_kek` |
| `webhook_seed_id` | `seed-1` | `seed-[a-z0-9-]{1,28}`; the key is the secret file `webhook_seed`, worker only (ADR 0004 §4; optional in M0, see [crypto.md](crypto.md)) |
| `webhook_pepper_id` | `pep-1` | `pep-[a-z0-9-]{1,28}`; the key is the secret file `webhook_pepper`, web and worker (optional in M0) |
| `smtp_host`, `smtp_port`, `smtp_sender`, `smtp_tls`, `smtp_username` | unset, 587, `ownvitals@localhost`, `starttls` | `smtp_tls=none` only for loopback or `mailpit` |
| `oauth_google_client_id` | unset | Google is the only OAuth provider |
| `worker_poll_seconds`, `worker_maintenance_seconds`, `worker_shutdown_wait_seconds` | 2, 30, 25 | wait at most 25 s |
| `job_lease_seconds`, `worker_concurrency`, `jobs_per_user`, `job_busy_requeue_limit` | 120, 2, 2, 5 | bounded: 30..600, 1..16, 1..100, 0..100 |
| `experimental_findings`, `ai_insights` | `false` | environment side of the stored flags; the stricter wins |

Secret files (`SECRET_FILE_FIELDS`): `database_password`, `database_owner_password`,
`database_maint_password`, `app_secret_key` (at least 32 bytes), `smtp_password`,
`oauth_google_client_secret`. The key ring's files (`connection_kek`, `webhook_seed`,
`webhook_pepper` and their retired `<file>_<id>`) are read by the crypto package, not by
`Settings` ([crypto.md](crypto.md)).

`Settings.sqlalchemy_url()` and `migrations_sqlalchemy_url()` return the URL with the password from
the file substituted when one exists.

**Database URL query parameters in `prod`** (`config.PROD_QUERY_PARAMETERS`, compared exactly):
`application_name`, `channel_binding`, `require_auth`, `ssl_min_protocol_version`, `sslcrl`,
`sslcrldir`, `sslmode`, `sslrootcert`, `sslsni`: verifying the server and labelling the connection.
Anything else is refused, among them `password`, `passfile`, `sslpassword`, `conninfo` and `dsn`
(a password or a whole connection string from somewhere other than the secret file), `service` (a
service file picks host, user and password), `options` (`-c` sets session settings, `app.user_id`
among them), `host` and `hostaddr`. The error names the field, never the URL or the parameter.
`dev` and `test` accept any parameter.

**The Postgres server must report its errors in English** (`lc_messages` `en_US` or `C`, the
Postgres default and the official image's; don't change it). psycopg reports no SQLSTATE for an
error at connect, so web and the worker tell a refused login (a wrong password, role or database
name, no CONNECT privilege, a forbidden connection option) from a database that is down by the
server's message, and a refused login stops startup with `DatabaseRoleError`
([isolation.md](isolation.md)). In another language a refusal reads as "unreachable": startup
goes on with every connection still refused, and the role check logs a warning naming the
`FATAL:` answer's SQLSTATE class (never its text).

## Migrations

Alembic lives in `backend/migrations` (`alembic.ini`). It connects as the owner role with
`OWNVITALS_MIGRATIONS_DATABASE_URL` (password from the `database_owner_password` file), never
through the app role. Migrations are numbered `NNNN_name`; take the next free number when the PR
opens and renumber on a rebase if another one merged first.

```bash
OWNVITALS_MIGRATIONS_DATABASE_URL=$OWNVITALS_TEST_DATABASE_URL uvx uv@0.13.6 run alembic upgrade head
uvx uv@0.13.6 run alembic upgrade head --sql      # offline: print the SQL
uvx uv@0.13.6 run alembic downgrade base
```

`0001_baseline` creates no tables; `0002_close_alembic_version` closes Alembic's own table to the
app role; `0003_jobs` adds the job tables ([jobs.md](jobs.md)) and `0004_connection_keks` the key
ring's check table ([crypto.md](crypto.md)). Every migration needs a working `downgrade` (the
`test_migrations.py` test upgrades to head and downgrades to base). Owned and system tables follow [isolation.md](isolation.md).

## Layout

| Path | What |
|---|---|
| `__main__.py` | `python -m ownvitals`: uvicorn with JSON logs, access log and proxy headers off, 5 s graceful shutdown; or a subcommand |
| `cli.py` | subcommand registry (`add_subcommands`, `COMMANDS`, `run_command`); later issues add theirs here |
| `config.py` | settings (above) |
| `log.py` | JSON logs and the redaction filter: cookies, credential headers, OAuth query strings and `code`/`state`, mail-link tokens, the secret part of `/api/webhooks/<provider>/<secret>`, secret keys in `extra`; strings in dict **keys** too; DB errors reduce to class and SQLSTATE. It fails closed: a record that cannot be formatted becomes a line with the redacted message text, the logger and the error type (`log_error`), and a failing write prints only the error type to stderr (`SafeStreamHandler`), never the raw message or arguments |
| `headers.py` | `Content-Security-Policy` (`'self'` only), `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff` on every response |
| `web.py` | app factory; refuses to start as a privileged database role; `/api/health` (no database), `/api/ready` (`SELECT 1`, 503 without detail); serves the built SPA from `static_dir`; includes `FEATURE_ROUTERS` |
| `emailnorm.py` | email normalisation (trim, ASCII lower case) |
| `db/engine.py` | async engine (role check on every connection, `DISCARD ALL` on pool return), the migrations engine, session factory, readiness query |
| `context.py`, `db/session.py`, `db/rls.py`, `db/tables.py`, `db/role_check.py` | `UserContext`, `user_session`/`system_session`, the RLS migration helper, `SYSTEM_TABLES`, the startup refusal: see [isolation.md](isolation.md) |
| `auth/`, `connections/`, `flags/`, `jobs/`, `admin/` | packages with an empty `routes.py` router that `web.py` already includes; the issues that own them fill them |
