# syntax=docker/dockerfile:1

# ---- Builder: install locked deps + the project into a venv ----
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first (this layer is cached while the lockfile is unchanged).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Then install the project itself (no dev group in the runtime image).
COPY . .
RUN uv sync --frozen --no-dev

# ---- Runtime: slim image with just the venv + source ----
FROM python:3.14-slim-bookworm AS runtime

# Non-root user + a writable data dir (used by the SQLite default; Postgres needs none).
RUN useradd --create-home --uid 1000 app \
    && mkdir /data \
    && chown app:app /data

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    # Containers are production: enables the fail-fast config checks and switches
    # schema management from create_all to Alembic migrations (run on start).
    ENVIRONMENT="production" \
    # Default to SQLite on the writable /data volume; override for Postgres.
    DATABASE_URL="sqlite+aiosqlite:////data/gateway.db"

USER app
EXPOSE 8000

# Liveness probe: /health does not touch the database.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health').read()" || exit 1

# Applies migrations, then serves. --proxy-headers + --forwarded-allow-ips expose
# the real client IP (needed for the per-IP rate limiting); in production replace
# "*" with your proxy's address.
CMD ["sh", "/app/docker-entrypoint.sh"]
