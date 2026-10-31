# ADR 0001: Stack, repository layout and the classbeat port rule

Status: Accepted (2026-10-31, #4)

## Context

PLAN D6 fixes the stack (classbeat's), §2 the architecture and the repository layout, and §3 says
which classbeat components are ported and how: "a copy, not a shared library", from a pinned
commit recorded here. M0 starts several developers in parallel (#2 backend, #3 frontend, #4 these
ADRs), so the details they share are decided once. classbeat ADR 0001 is the starting point;
this ADR keeps its shape and records where ownvitals differs: a home server behind Cloudflare and
SWAG instead of a VPS behind Caddy (§9.2), deploys pinned to attested digests, no MOWL library,
and health data (D7, §9.3) instead of workout plans.

## Decision

### 1. Stack (PLAN D6)

| Layer | Choice |
|---|---|
| Backend | Python **3.12**, **FastAPI** + uvicorn, **SQLAlchemy 2 async** with **psycopg 3** (`postgresql+psycopg://`), **Alembic** (async env), **pydantic v2** and pydantic-settings, **httpx** for outbound HTTP, **uv** with a committed `uv.lock` |
| Frontend | **React 19** + **Vite** + **TypeScript** (`strict`), Node **24**, npm with a committed `package-lock.json`, exact versions |
| Database | **PostgreSQL 17** (image pinned by patch version, `postgres:17.11` at M0) |
| Quality | ruff (lint and format), pyright, pytest + pytest-asyncio with `filterwarnings = ["error"]`; ESLint, Vitest + Testing Library |
| Not used | Redis, Kafka, Kubernetes, TimescaleDB, microservices (D6). Timescale's continuous aggregates don't support RLS, which ADR 0002 requires on every health table |

- Async end to end, as classbeat: server-sent events and `LISTEN/NOTIFY` (ADR 0005) share one
  event loop with request handling. No engine or session is created at import time; the app's
  lifespan and the worker's start create them.
- Database tests run against a real Postgres (`OWNVITALS_TEST_DATABASE_URL`). They skip locally
  when it is unset and **fail in CI** (`CI=true`) when it is missing, so a lost setting can't
  turn the RLS guards green (#2).
- No test reaches the network: an autouse network guard refuses any non-loopback connect before
  anything is sent (#2 `test_netguard.py`). Withings, Google and model APIs are faked.

### 2. Repository layout (PLAN §2)

| Path | Holds |
|---|---|
| `backend/` | uv project; package `ownvitals` in `backend/src/ownvitals/`; `tests/`; Alembic in `migrations/` with `alembic.ini` |
| `backend/src/ownvitals/` ported | `auth/ crypto/ connections/ db/ jobs/ worker/ admin/ flags/`, `context.py`, `egress.py`, `mail.py`, `log.py`, `headers.py`, `config.py`, `cli.py`, `keys_cli.py` |
| `backend/src/ownvitals/` new (M1+) | `withings/` (client, oauth, tokens, sync, webhooks, rate, capabilities); `ingest/ analysis/ ecg/ scores/ findings/ reports/ insights/` |
| `frontend/` | the SPA (API client, session and guards ported); built to `frontend/dist` |
| `e2e/` | Playwright against the compose stack with `fake-oidc` (ported) and `fake-withings` (M1) |
| `docker/` | files mounted into third-party containers: Postgres init (roles), queue SQL (ADR 0005), maint grants |
| `scripts/` | developer and CI scripts (`worktree.sh`, CI gate); the deploy script of §9.2 (M2) |
| `secrets/` | local secret files, gitignored, never committed |
| `docs/` | `roadmap.md` (the frozen plan), `status.md`, `adr/`, `security/`, `dev/`, `runbooks/` |
| `.claude/agents/` | the agent roles (`developer.md`, `reviewer.md`, `explorer.md`) |

### 3. HTTP surface and packaging

- JSON API under `/api`: `GET /api/health` (process up, no database) and `GET /api/ready`
  (database reachable; 503 otherwise, with no exception text). The web process serves the built
  SPA from `OWNVITALS_STATIC_DIR` and answers any other non-`/api` GET with `index.html`. **Same
  origin, so no CORS**, as PLAN §2 says.
- The Withings webhook (`/api/webhooks/withings/{secret}`, M1) and the OAuth callback
  (`/api/oauth/withings/callback`, §4.1) are routes of the web process like any other.
- **One image, two processes** (PLAN §2, classbeat ADR 0001): a Node stage builds the SPA (#14)
  and a `python:3.12-slim` stage installs the backend with uv and copies the SPA in. It runs as a
  non-root user.
  - web: uvicorn (`python -m ownvitals`);
  - worker: `python -m ownvitals.worker` (#9): jobs, scheduler and purge (PLAN §2);
  - `migrate`: a one-shot service running `alembic upgrade head` as the owner role before web and
    worker start; `db-functions`: a one-shot service that installs the queue functions as the
    superuser (ADR 0005 §3).
- `compose.yaml` services: `db`, `migrate`, `db-functions`, `web`, `worker`, `mailpit` (local
  mail), and under the `test` profile `fake-oidc` and `fake-withings` (§9.1). Published ports bind
  `127.0.0.1` locally. Postgres is never published, locally or on the home server.
- **Postgres is the only stateful service**: the job queue, its events, the rate-limit bucket and
  the webhook dedupe all live in it (PLAN §4.4, ADR 0005).

### 4. Settings and secrets

- Non-secret settings are environment variables prefixed `OWNVITALS_`. `.env` holds only local
  non-secret values (ports, compose project name) and is gitignored; `.env.example` is committed
  with placeholders only.
- **Secrets are files, never environment variables**: Docker secrets at `/run/secrets/<name>`
  (directory overridable with `OWNVITALS_SECRETS_DIR` for tests), named without a prefix
  (`database_password`, `database_owner_password`, `app_secret_key`, `connection_kek`, …), read by
  a small custom settings source. An environment variable never supplies a secret. In code they
  are `SecretStr`, never logged and never in a `repr` (#2 `test_config.py`).
- A database password replaces the password part of its URL at connect time, so no URL in the
  settings carries a secret.
- **ADRs name a new setting by its purpose and default** (for example "the step-up window,
  default 10 minutes"); the issue that implements it chooses the variable name, under the
  `config.py` lock of `docs/status.md`.
- **The KEK files and the webhook-secret pepper** (ADR 0004) are secret files like the others,
  mounted read-only; on the home server they live outside the database volume (§9.2 Hardening)
  and are backed up separately from the database dumps (§9.2 Backups).

### 5. The classbeat port rule (PLAN §3)

- **Pinned source:** `magpern/classbeat` at
  **`d80605100992f85f8f02672b9b5f885ed6166214`** (also recorded in `docs/roadmap.md` and
  `docs/status.md`). Every port issue names this commit and the files it copies; the reference
  checkout is read-only.
- **A copy, not a shared library.** Files are copied and renamed (`classbeat` → `ownvitals`,
  `CLASSBEAT_` → `OWNVITALS_`). ownvitals never imports, vendors as a package, or depends on
  classbeat at build or run time, so classbeat's later changes reach ownvitals only on purpose.
- **What is not ported** (PLAN §3, D11): `plans/`, `songs/`, `spotify/`, `design/`,
  `playlists/`, `classes/`, `saving/`, `operations/`, `models/`, `covers.py`, `mowl_state.py`, the
  MOWL and Swedebeat code and the `mowl-workouts` wheel, and quota code until M7 (the per-user AI
  cost cap, without tiers or codes). The agent provider layer and evals arrive in M7 (§7.4).
- **Port, then change.** A port PR keeps classbeat's behaviour and tests so the diff against the
  pinned commit stays reviewable, and the ownvitals-specific changes (mandatory TOTP, step-up,
  token generations, …) land in their own issues (for example #10 then #11).
- **Health-data review of every ported component** (PLAN §3): the threat model
  ([docs/security/threat-model.md](../security/threat-model.md)) re-assesses each one; #16 checks
  it against the merged code.
- **Security fixes are ported deliberately.** The pin does not move. At the end of every
  milestone the Architect reads classbeat's history since the last check for security-relevant
  changes and opens one ownvitals issue per fix worth porting; the PR names the classbeat commit
  it ports from, and `docs/status.md` ("External dependencies") records the last commit checked.
  A fix that does not apply (code not ported) is recorded as such. This answers PLAN §12's
  "Divergence from classbeat".

### 6. Hosting shape (PLAN-operations §9.1, §9.2)

| Environment | Shape |
|---|---|
| Local (Windows, Docker Desktop) | compose with fakes and the demo account only; **no real health data** on the development machine, where agents run |
| Home server (household phase) | compose project `ownvitals` on 192.168.1.222, web published on `8096` and reachable only from SWAG (192.168.1.151), which serves `withings.pernemark.se` behind Cloudflare; image pulled **by digest** from GHCR through the forced-command deploy user (§9.2) |
| Beyond the household | an EU VPS before any non-household user (D8, M8) |

- There is no Caddy (classbeat's VPS proxy): TLS ends at Cloudflare (if proxied) and at SWAG.
  The trusted-proxy list for the client IP is SWAG's address, with `CF-Connecting-IP` honoured
  only on requests that came through SWAG (ADR 0003 §8).
- Releases: a protected `v*` tag builds the image in `release.yml` with a build-provenance
  attestation (#7); the deploy script accepts only an attested digest from such a tag (§9.2).
  Whether the GHCR package is public is the owner's decision; the image holds no secret either
  way.
- No self-hosted CI runner on the home server (§10.5).

## Alternatives

- **A shared classbeat library (package or submodule)**: ownvitals would inherit every classbeat
  change, including product code it doesn't want, and a classbeat refactor could weaken a
  health-data control without an ownvitals review. Rejected by PLAN §3.
- **Tracking classbeat `main`**: same problem; the pin plus deliberate ports keeps every change
  reviewed against ownvitals' threat model.
- **TimescaleDB for the time series**: continuous aggregates don't support RLS (D6). Monthly
  native partitions are used instead, under the RLS rules of ADR 0002 §2 (PLAN §5).
- **Separate web and worker images**: two builds to keep in step for no isolation gain; they run
  the same code (classbeat ADR 0001).
- **Django, sync SQLAlchemy, Redis/Celery, Poetry**: rejected for classbeat's reasons (classbeat
  ADR 0001, Alternatives); nothing in ownvitals changes them.

## Consequences

- Every PR builds on this layout; changing it needs a new ADR.
- Secrets as files require `scripts/worktree.sh init` (#2) to create `secrets/*` before the first
  start, and the e2e stack check (#14) to prove no secret value appears in `docker inspect`.
- The pinned commit is the reference for every port issue in M0 (#2, #3, #5 to #14) and for
  later ports (M7 provider layer and cost cap).
- The Architect owns the milestone-end check of classbeat's history (§5).
