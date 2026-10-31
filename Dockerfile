# syntax=docker/dockerfile:1.27.1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# One image for the web server and, later, the worker. Every image the build pulls is pinned by
# tag and index digest (`name:tag@sha256:...`: the digest decides, the tag says what it is).
# Dependabot's docker entry bumps the python image's tag and digest. The uv image moves by hand
# with setup-uv (scripts/ci-uv-version.sh checks the tags agree), and so does the syntax line,
# which Dependabot does not read (docs/dev/release.md).
# The single-page-app build stage arrives with the end-to-end harness issue.

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

# --- runtime ----------------------------------------------------------------------------
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS runtime

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    OWNVITALS_ENVIRONMENT=prod
# The image fails closed: prod (https public URL, no API docs, no password inside a database URL)
# unless the stack sets OWNVITALS_ENVIRONMENT itself, as compose.yaml does for the local stack.

RUN groupadd --system --gid 10001 ownvitals \
    && useradd --system --uid 10001 --gid ownvitals --no-create-home --shell /usr/sbin/nologin ownvitals

WORKDIR /app
# Dependencies first so this layer is cached until the lockfile changes.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY backend/src ./src
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

USER ownvitals
EXPOSE 8000
CMD ["python", "-m", "ownvitals", "--host", "0.0.0.0", "--port", "8000"]
