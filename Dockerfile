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

# ---- Docs: build the MkDocs site the app serves at /docs ----
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS docs

ENV UV_LINK_MODE=copy

WORKDIR /app

# The dev group holds mkdocs; the project itself isn't needed to build the docs.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Reproduce `just docs-prepare` (the source projection mkdocs.yml's docs_dir
# points at) then build the site to /app/.mkdocs-site (mkdocs.yml site_dir).
COPY . .
RUN mkdir -p .mkdocs-docs \
    && ln -sfn ../README.md .mkdocs-docs/index.md \
    && ln -sfn ../EXAMPLES.md .mkdocs-docs/EXAMPLES.md \
    && ln -sfn ../CONTRIBUTING.md .mkdocs-docs/CONTRIBUTING.md \
    && ln -sfn ../SECURITY.md .mkdocs-docs/SECURITY.md \
    && ln -sfn ../docs .mkdocs-docs/docs \
    && ln -sfn ../issues .mkdocs-docs/issues \
    && uv run mkdocs build --strict

# ---- UI: build the admin console (SPA) the app serves at /ui ----
FROM node:22-bookworm-slim AS ui

WORKDIR /app/ui

# Pin pnpm to the version the lockfile was written with; npm ships with node.
RUN npm install --global pnpm@11.6.0

# Install deps first (this layer is cached while the lockfile is unchanged).
# strict-dep-builds=false: in a clean, non-interactive environment pnpm 10.16+
# otherwise fails (ERR_PNPM_IGNORED_BUILDS) on esbuild's skipped build script.
# We don't need that script — esbuild's platform binary ships via an optional
# dependency — so the vite build works regardless; this just keeps install green.
COPY ui/package.json ui/pnpm-lock.yaml ui/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile --config.strict-dep-builds=false

# Then build the SPA to /app/ui/dist (vite `base: '/ui/'`). Local node_modules
# and dist are .dockerignored, so this copies only source and won't clobber the
# freshly installed node_modules above.
COPY ui/ ./
RUN pnpm run build

# ---- Runtime: slim image with just the venv + source ----
FROM python:3.14-slim-bookworm AS runtime

# Non-root user + a writable data dir (only used if a non-production run opts
# into SQLite explicitly; production requires Postgres and needs no volume).
RUN useradd --create-home --uid 1000 app \
    && mkdir /data \
    && chown app:app /data

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

# The built narrative docs, served by the app as static files at /docs (see
# infrastructure/web/docs_site.py). Absent when running from source; present here.
COPY --from=docs --chown=app:app /app/.mkdocs-site /app/.mkdocs-site

# The built admin console, served by the app as static files at /ui (see
# infrastructure/web/ui_site.py). Absent when running from source; present here.
COPY --from=ui --chown=app:app /app/ui/dist /app/ui/dist

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
