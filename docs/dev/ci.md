# CI

Moved out of [AGENTS.md](../../AGENTS.md) so every agent does not read it on every start; the text is unchanged apart from relative links. The one rule that stays in AGENTS.md is how to handle a new advisory (the `ci.yml` comments cite it).

| Workflow / job | Runs | What it does |
|---|---|---|
| `ci` / `changes` | always | `scripts/ci-changes.sh`: which areas the PR changed (`backend`, `frontend`, `stack`, `all`); `Dockerfile`, `.dockerignore`, `compose*.yaml`, `docker-compose*.yml`, `.env.example`, `deploy/` and `docker/` set `backend` too (its tests guard them); a change to `.github/`, `.gitattributes` or the CI scripts, the weekly run and a manual run set `all`; a diff that can't be computed runs everything |
| `ci` / `checks` | always | Frozen-spec guard (the three spec files equal `635901d`); gitleaks over the PR's own commits (weekly: whole history); `bash -n` and shellcheck on `scripts/*.sh`, `deploy/ownvitals-deploy` and `docker/postgres/**/*.sh` (`sh -n` for those); the CI gate and change-filter tests (`scripts/test-ci-gate.sh`); the release workflow tests (`scripts/test-release.sh`) and the download checksum tests (`scripts/test-ci-downloads.sh`); actionlint; the uv version check (`scripts/ci-uv-version.sh`); pip-audit; npm audit; Trivy repository scan |
| `ci` / `backend` | `backend` | From `backend/`: `uv sync --locked`, ruff, format check, pyright, pytest against a Postgres 17 service. No step skips: a missing `backend/`, `pyproject.toml` or `uv.lock` fails the job |
| `ci` / `frontend` | `frontend` | From `frontend/`: `npm ci`, lint, typecheck, Vitest, build. No step skips: a missing `frontend/` or `package-lock.json` fails the job |
| `ci` / `ci` | always | The required check (`scripts/ci-gate.sh`): `changes` and `checks` succeeded, and every other job succeeded or was skipped while its filter is false. Fails closed |
| `pr-title` | every PR title change | commitlint (`commitlint.config.cjs`) on the title with ` (#<n>)` appended, after a self-test |

A new job joins `needs` of `ci`, `JOBS`/`FILTERS` in `scripts/ci-gate.sh`, the gate tests and
this table in the same PR.

Every GitHub Action is pinned by full commit SHA with the version in a comment. Release
binaries downloaded in CI (gitleaks, Trivy) are checksum-verified (`scripts/test-ci-downloads.sh`
tests it), and the shellcheck and actionlint images are pinned by digest. commitlint runs through
`npx` at exact versions (no lockfile), and the Postgres service image is pinned by tag and index
digest, the same reference as `compose.yaml`'s `db` (a test checks they agree).
Dependabot keeps Actions, uv, npm and the Docker images current; what it can't see is bumped by
hand ([docs/dev/release.md](release.md)).

The dependency audits are steps of `checks` and also run in the weekly run, which picks up new
advisories:

- **uv version:** the `version:` of every `setup-uv` step (a literal, never `version-file` or an
  expression) and every `uv@X.Y.Z` / `with uv X.Y.Z` in `docs/dev/backend.md` equal the uv tag of the
  `Dockerfile`; the check needs yq v4, which the runner image has.
- **pip-audit:** its exact version is the `audit` dependency group of `backend/pyproject.toml`, so its
  own dependencies are hash-locked in `uv.lock`; CI installs it with `uv sync --frozen --only-group
  audit` and the runtime image (`uv sync --locked --no-dev`) never has it. It audits `uv export
  --frozen` with hashes (`--require-hashes --strict`): production dependencies, then every group.
- **npm audit:** `npm audit --audit-level=high` in `frontend/` (and `e2e/` once it exists). The
  moderate, dev-only findings of the ESLint chain do not fail it.
- **Trivy repository scan:** vulnerabilities in the lockfiles and misconfiguration in the
  `Dockerfile` (compose is not scanned), failing on HIGH and CRITICAL findings that have a fix
  (`--ignore-unfixed`). **Accepted risk:** unfixed HIGH and CRITICAL findings are not gated; they
  are revisited at the M8 security review, and once a fix appears Trivy starts failing on them.

