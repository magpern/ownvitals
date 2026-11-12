# The local stack and worktrees

`compose.yaml` runs the stack on one machine: **db** (`postgres:17.11`, pinned by its index
digest, which creates the database roles on a new volume), **migrate** (a one-shot
`alembic upgrade head` as the owner role), **db-functions** (a one-shot that installs the job
queue's `SECURITY DEFINER` functions as the superuser, from the application image), **web** (the
API as the app role, non-root user `ownvitals`, uid 10001) and **worker**. A later issue adds the
mail service. The stack only ever
holds synthetic data (the demo account and fakes): never connect a real Withings account to it.
The one exception is the owner's own local stack on the owner's own computer
([local-real-account.md](local-real-account.md), AGENTS.md "Health data").

Ports bind `127.0.0.1` only and come from `.env`. Passwords and keys are files under `secrets/`
mounted as Docker secrets (`/run/secrets/<name>`), never environment variables. `.env` and
`secrets/` are git-ignored; `.env.example` holds placeholders only.

## One checkout

```bash
scripts/worktree.sh init            # .env (from .env.example) and secrets/ with random values, if missing
docker compose up -d --build --wait # db, then migrate, then web; returns when web is healthy
curl -s http://127.0.0.1:8000/api/ready    # {"status":"ready"}
curl -s http://127.0.0.1:8000/api/health   # {"status":"ok","version":"0.1.0"}
docker compose exec web id -u              # 10001: not root
docker compose down -v                     # stop and delete the database volume
```

`init` is idempotent and never overwrites an existing `.env` or secret. Default ports: web 8000,
Postgres 55432, mail UI 8025.

## One stack per worktree

Each issue works in its own worktree with its own compose project, database volume and ports, so
stacks run side by side:

```bash
scripts/worktree.sh new <issue> <slug>   # ../ownvitals-wt/<issue>-<slug> from origin/main, then init <issue>
scripts/worktree.sh ls                   # worktrees with project name and ports
scripts/worktree.sh rm <issue>           # compose down -v for that project, remove the worktree, delete the branch if merged
```

| Value | For issue `n` |
|---|---|
| `COMPOSE_PROJECT_NAME` | `ownvitals-<n>` |
| web | `127.0.0.1:20000 + n` |
| Postgres | `127.0.0.1:30000 + n` |
| mail UI | `127.0.0.1:40000 + n` |

`n` is a number from 1 to 9999 without leading zeros. Inside an existing worktree,
`scripts/worktree.sh init <n>` writes the same `.env` for it. Worktrees live next to the main
checkout in `../ownvitals-wt/`. `rm` refuses a worktree with uncommitted or untracked work, only
ever touches the project `ownvitals-<n>` (whatever the worktree's `.env` says), and keeps a branch
git does not see as merged (squash merges never are; delete it with `git branch -D` after the PR
merged).

A throwaway test Postgres for `pytest` (see [backend.md](backend.md)) may use any free port. If it
uses the stack's own Postgres port (`30000 + n`), stop it before starting the stack.

## Secrets

`scripts/worktree.sh init` creates, with random values, `postgres_password`,
`database_owner_password`, `database_password`, `database_maint_password`, `app_secret_key` (64
bytes, hex) and `connection_kek` (32 bytes, 64 lowercase hex characters). They are `0644` so the
container user can read the bind mount; `secrets/` itself is `0700`. The image contains none of
them; `docker inspect` shows only the file mounts.

`init` also creates an **empty** `anthropic_api_key` (never a generated key): Compose needs the
file to mount it, and an empty key is `ai_not_configured` ([ai.md](ai.md)). The worker alone
mounts it, with `app_secret_key` (the AI audit key); `test_compose_ai_key.py` fails if any
compose file at the root gives the key to another service (a secrets entry by source or target,
an alias secret, an environment-backed secret or variable, or a bind mount that is or may hold
`secrets/`). A checkout made before this needs `scripts/worktree.sh init` again, and the staging
host needs the file too (empty keeps AI off).

The database roles ([isolation.md](isolation.md)): `db` creates `ownvitals_owner`,
`ownvitals_app` and the `ownvitals_maint` LOGIN role from `docker/postgres/init/10-roles.sql`
on the first start of an empty volume, with the passwords of `database_owner_password`,
`database_password` and `database_maint_password` (an older volume needs
`docker compose down -v`). `migrate` connects as `ownvitals_owner` with
`database_owner_password`, `web` and `worker` as `ownvitals_app` with `database_password`.
`web` alone also has `OWNVITALS_MAINT_DATABASE_URL`
(`postgresql+psycopg://ownvitals_maint@db:5432/ownvitals`) and the `database_maint_password`
secret, so with both set the admin routes do not answer 503 `admin_unavailable`. The worker
does not get that URL or secret. Only `db` and `db-functions` hold `postgres_password`
(`db-functions` writes it to a `PGPASSFILE` on a tmpfs, [jobs.md](jobs.md)). Web and the worker
refuse to start as a privileged role.

## Where the database's SQL comes from

The SQL the services run comes from the application image, never from a host path, with one
exception:

| SQL | Runs | From |
|---|---|---|
| `docker/postgres/queue/20-queue.sql` (the queue's role and definer functions), then `docker/postgres/maint/30-maint.sql` (the admin role's attributes and allowlist) | `db-functions`, on every start, both files in one transaction through `ownvitals.db.install_functions` | the application image (`/app/db-functions`), so on the server the deployed release's digest: a deploy installs that release's functions and a rollback re-installs the previous release's ([deploy.md](deploy.md), "Database functions") |
| migrations | `migrate` | the application image |
| `docker/postgres/init/10-roles.sql` (the owner and app roles, grants and defaults; the maint LOGIN role and its password) | `db`'s own init, **only on the first start of an empty volume** | the host: `./docker/postgres/init:/docker-entrypoint-initdb.d:ro` |

The init mount is the one allowed exception, by its exact source and target. It runs once, when
the volume is created, and never again on a deploy, a rollback or a restart; it creates the
roles, not the definer functions a deploy updates, and it needs psql's meta-commands (it reads
the password files), which only the postgres image has. A change to it reaches an existing
database only by hand (or `docker compose down -v` locally). `test_compose_host_sql.py` fails if
any other service, mount or secret file reads a host path that holds SQL (a path it cannot
inspect counts as one). It parses every compose file at the repository root (`compose*.y*ml`,
`docker-compose*.y*ml`) as YAML, in every volume spelling, and fails closed on what Compose could
use to bring in another file: `include`, `extends`, `develop`, `configs`, `volumes_from`, a named
volume with driver options, an override file Compose merges by default
(`compose.override.y*ml`, `docker-compose.override.y*ml`) and `COMPOSE_FILE` in `.env.example`.
The repository's compose uses none of them. A change to any of these files runs the backend job
(`scripts/ci-changes.sh`).

## Image

`Dockerfile` builds the backend only: `ghcr.io/astral-sh/uv:0.13.6` installs the locked
dependencies (`uv sync --locked --no-dev`) into `python:3.12.16-slim-bookworm`, then the app runs as
the non-root user `ownvitals`: `python -m ownvitals --host 0.0.0.0 --port 8000`. It also carries the
queue functions, `30-maint.sql` and their entrypoint under `/app/db-functions` (`db-functions` runs
them in one transaction, `20-queue.sql` then `30-maint.sql`). The
single-page-app build stage is added with the end-to-end harness. `.dockerignore` keeps `.git`, `.env*`,
`secrets/`, virtualenvs, caches and docs out of the build context.

The image **defaults to `OWNVITALS_ENVIRONMENT=prod`**, so a container started without settings
fails closed: it needs an `https://` public URL, serves no API docs and refuses a password inside a
database URL. `compose.yaml` sets `OWNVITALS_ENVIRONMENT: dev` for the local stack explicitly, and
its database URLs carry the user only (the password is a secret file); `test_image.py` checks both.

## Staging on the home server

`https://ownvitals.pernemark.se` (roadmap Amendment 9) runs the repository's `compose.yaml` built
from source on 192.168.1.222, as its own project, with an override file kept out of git
(`compose.staging.yaml`, listed in `.git/info/exclude`):

```bash
docker compose -p ownvitals-staging -f compose.yaml -f compose.staging.yaml up -d --build --wait
```

The override replaces the published ports (`ports: !override`: Compose otherwise appends), moves
web to `192.168.1.222:8098` and the database and mail UI to other `127.0.0.1` ports (the host's
other projects hold the defaults), sets `OWNVITALS_ENVIRONMENT=prod`, the public URL, the Google
client id and, on web only, the SMTP settings (`smtp.protonmail.ch:587`, STARTTLS, the secret file
`smtp_password`). It sets no `OWNVITALS_TRUSTED_PROXIES`, so the app sees SWAG's address as the
client (Amendment 9). It also mounts what `compose.yaml` does not: `connection_kek` into the
worker (it loads the key ring at start), `webhook_seed` (worker only) and `webhook_pepper`, and
`oauth_google_client_secret` and `smtp_password` into web.

**Secret files readable by the image user.** Outside Swarm, Compose bind-mounts a file secret with
its host owner and mode and ignores `uid`, `gid` and `mode`, so uid 10001 must be able to read the
host file. The local stack uses `0644` files in a `0700` directory. Where a secret should not be
world-readable (the SMTP token), make it `<owner>:ownvitals-secrets` mode `0640` (a system group
with no members, gid 10011, created with `groupadd --system`) and give the service
`group_add: ["10011"]`, as classbeat does with its own group. A `0600` file the owner holds makes
the service fail at start with `PermissionError` on `/run/secrets/<name>`.
