# The local stack and worktrees

`compose.yaml` runs the stack on one machine: **db** (`postgres:17.11`, pinned by its index
digest, which creates the database roles on a new volume), **migrate** (a one-shot
`alembic upgrade head` as the owner role), **db-functions** (a one-shot that installs the job
queue's `SECURITY DEFINER` functions as the superuser, from the application image), **web** (the
API as the app role, non-root user `ownvitals`, uid 10001) and **worker**. A later issue adds the
mail service. The stack only ever
holds synthetic data (the demo account and fakes): never connect a real Withings account to it.

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

The database roles ([isolation.md](isolation.md)): `db` creates `ownvitals_owner` and
`ownvitals_app` from `docker/postgres/init/10-roles.sql` on the first start of an empty volume,
with the passwords of `database_owner_password` and `database_password` (an older volume needs
`docker compose down -v`). `migrate` connects as `ownvitals_owner` with `database_owner_password`,
`web` and `worker` as `ownvitals_app` with `database_password`; only `db` and `db-functions` hold
`postgres_password` (`db-functions` writes it to a `PGPASSFILE` on a tmpfs, [jobs.md](jobs.md)).
Web and the worker refuse to start as a privileged role.

## Where the database's SQL comes from

The SQL the services run comes from the application image, never from a host path, with one
exception:

| SQL | Runs | From |
|---|---|---|
| `docker/postgres/queue/20-queue.sql` (the queue's role and definer functions) | `db-functions`, on every start | the application image (`/app/db-functions`), so on the server the deployed release's digest: a deploy installs that release's functions and a rollback re-installs the previous release's ([deploy.md](deploy.md), "Database functions") |
| migrations | `migrate` | the application image |
| `docker/postgres/init/10-roles.sql` (the two roles, grants, defaults) | `db`'s own init, **only on the first start of an empty volume** | the host: `./docker/postgres/init:/docker-entrypoint-initdb.d:ro` |

The init mount is the one allowed exception, by its exact source and target. It runs once, when
the volume is created, and never again on a deploy, a rollback or a restart; it creates the
roles, not the definer functions a deploy updates, and it needs psql's meta-commands (it reads
the password files), which only the postgres image has. A change to it reaches an existing
database only by hand (or `docker compose down -v` locally). `test_compose_roles.py` fails if
any other service, mount or secret file reads a host path that holds SQL (a path it cannot
inspect counts as one), if a mount uses the long volume syntax, or if a named volume gets driver
options.

## Image

`Dockerfile` builds the backend only: `ghcr.io/astral-sh/uv:0.13.6` installs the locked
dependencies (`uv sync --locked --no-dev`) into `python:3.12.16-slim-bookworm`, then the app runs as
the non-root user `ownvitals`: `python -m ownvitals --host 0.0.0.0 --port 8000`. It also carries the
queue functions and their entrypoint under `/app/db-functions` (`db-functions` runs them). The
single-page-app build stage is added with the end-to-end harness. `.dockerignore` keeps `.git`, `.env*`,
`secrets/`, virtualenvs, caches and docs out of the build context.

The image **defaults to `OWNVITALS_ENVIRONMENT=prod`**, so a container started without settings
fails closed: it needs an `https://` public URL, serves no API docs and refuses a password inside a
database URL. `compose.yaml` sets `OWNVITALS_ENVIRONMENT: dev` for the local stack explicitly, and
its database URLs carry the user only (the password is a secret file); `test_image.py` checks both.
