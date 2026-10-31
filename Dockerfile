# syntax=docker/dockerfile:1
# One image for the web server and, later, the worker. Base images are pinned by exact tag.
# The single-page-app build stage arrives with the end-to-end harness issue.

FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

# --- runtime ----------------------------------------------------------------------------
FROM python:3.12.16-slim-bookworm AS runtime

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

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
