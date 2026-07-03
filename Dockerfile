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

# Non-root user + a writable data dir (only used if a non-production run opts
# into SQLite explicitly; production requires Postgres and needs no volume).
RUN useradd --create-home --uid 1000 app \
    && mkdir /data \
    && chown app:app /data

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

# No DATABASE_URL default on purpose: production requires PostgreSQL, and the
# app fails fast at startup unless DATABASE_URL points at postgresql+asyncpg://
# (a SQLite default here would silently give every replica its own database).
ENV PATH="/app/.venv/bin:$PATH" \
    # Containers are production: enables the fail-fast config checks and switches
    # schema management from create_all to Alembic migrations (run on start).
    ENVIRONMENT="production"

USER app
EXPOSE 8000

# Liveness probe: /health does not touch the database.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health').read()" || exit 1

# Applies migrations, then serves. Forwarded headers are trusted only from
# FORWARDED_ALLOW_IPS (default: loopback) — set it to your reverse proxy's
# IP/CIDR so the real client IP reaches the per-IP rate limiting.
CMD ["sh", "/app/docker-entrypoint.sh"]
