# justfile — task runner for the Litestar LLM gateway.
# Run `just` (or `just --list`) to see all recipes.
# Requires: just (https://github.com/casey/just) and uv (https://docs.astral.sh/uv/).

# App entrypoint used by the Litestar CLI (migrations, dev server).
app := "litestar_gateway.app:app"

# Show the list of recipes (default when running `just` with no args).
default:
    @just --list

# ── Setup ────────────────────────────────────────────────────────────────────

# Install/sync dependencies exactly as locked (matches CI's `uv sync --frozen`).
install:
    uv sync --frozen

# Install the git pre-commit hooks so they run on every commit.
hooks-install:
    uv run pre-commit install

# ── Quality: lint, format, types ───────────────────────────────────────────────

# Lint with ruff (no changes; same check CI runs).
lint:
    uv run ruff check

# Lint and auto-fix what ruff can fix safely.
lint-fix:
    uv run ruff check --fix

# Format the code in place.
format:
    uv run ruff format

# Verify formatting without writing (CI-style gate).
format-check:
    uv run ruff format --check

# Static type check with pyrefly.
typecheck:
    uv run pyrefly check

# Run all pre-commit hooks on every file (lint, format, secrets, hygiene).
pre-commit:
    uv run pre-commit run --all-files --show-diff-on-failure

# ── Tests ──────────────────────────────────────────────────────────────────────

# Run the test suite. Extra args pass through, e.g. `just test -k organizations`.
test *args:
    uv run pytest -q {{args}}

# Run the Postgres CI checks locally (mirrors the CI `postgres` job): spin up a
# throwaway Postgres 17, apply the real migration chain (the same command the
# Docker entrypoint runs on deploy), then run the FULL suite against it — every
# DB-backed fixture takes its own throwaway database via the root `database_url`
# fixture, so Postgres-vs-SQLite differences surface across the whole suite.
# Requires Docker; the container is always removed on exit. ENVIRONMENT stays
# "development" so Settings needs only DATABASE_URL, exactly like the CI job.
test-postgres:
    #!/usr/bin/env bash
    set -euo pipefail
    name=lsg-ci-pg
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker run -d --name "$name" -e POSTGRES_USER=gateway -e POSTGRES_PASSWORD=gateway -e POSTGRES_DB=gateway -p 5433:5432 postgres:17 >/dev/null  # pragma: allowlist secret
    trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
    echo "waiting for postgres to accept connections…"
    until docker exec "$name" pg_isready -U gateway >/dev/null 2>&1; do sleep 1; done
    export DATABASE_URL="postgresql+asyncpg://gateway:gateway@localhost:5433/gateway"  # pragma: allowlist secret
    uv run litestar --app {{app}} database upgrade --no-prompt
    uv run pytest -q

# Run the GitHub Actions `ui` job locally.
ui-ci:
    #!/usr/bin/env bash
    set -euo pipefail
    export CI=true
    cd ui
    pnpm install --frozen-lockfile --config.strict-dep-builds=false
    pnpm test
    pnpm lint
    pnpm build

# Run the GitHub Actions `docker` job locally: build the production image, boot
# it with the CI configuration, and exercise the same /health smoke test.
docker-ci:
    #!/usr/bin/env bash
    set -euo pipefail
    name=lsg-citest
    image=lsg-citest
    port="${DOCKER_CI_PORT:-18000}"
    database_url="sqlite+aiosqlite:////data/citest.db"  # pragma: allowlist secret
    master_key="change-me-please"  # pragma: allowlist secret
    salt_key="change-me-strong-random"  # pragma: allowlist secret
    cleanup() {
        if docker inspect "$name" >/dev/null 2>&1; then
            docker logs "$name" || true
            docker rm -f "$name" >/dev/null || true
        fi
    }
    trap cleanup EXIT
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker build -t "$image" .
    docker run -d --name "$name" \
        -p "$port:8000" \
        -e ENVIRONMENT=development \
        -e DATABASE_URL="$database_url" \
        -e MIGRATE_ON_START=false \
        -e MASTER_KEY="$master_key" \
        -e SALT_KEY="$salt_key" \
        "$image"
    for _ in $(seq 1 30); do
        if curl -fsS "http://127.0.0.1:$port/health"; then
            echo "health check passed"
            exit 0
        fi
        sleep 1
    done
    echo "container failed to become healthy" >&2
    exit 1

# Run every GitHub Actions PR job locally: Python checks/coverage, UI,
# production-dialect Postgres, and the built-container health smoke test.
pr-coverage:
    uv run pre-commit run --all-files
    uv run pyrefly check
    uv run --with pip-audit pip-audit
    uv run pytest -q --cov=src/litestar_gateway --cov-report=term --cov-fail-under=80
    just ui-ci
    just test-postgres
    just docker-ci

# ── Migrations (advanced-alchemy / Alembic via the Litestar CLI) ────────────────

# Apply all pending migrations (idempotent; no-op when already at head).
migrate:
    uv run litestar --app {{app}} database upgrade --no-prompt

# Create a new migration revision. Usage: `just make-migration "add users table"`.
make-migration message:
    uv run litestar --app {{app}} database make-migrations -m "{{message}}" --no-prompt

# Downgrade to a revision (default: one step back). e.g. `just downgrade base`.
downgrade revision="-1":
    uv run litestar --app {{app}} database downgrade {{revision}} --no-prompt

# Show the current revision applied to the database.
migration-current:
    uv run litestar --app {{app}} database show-current-revision

# List migration history in chronological order.
migration-history:
    uv run litestar --app {{app}} database history

# Check whether the database schema is up to date with the migrations.
migration-check:
    uv run litestar --app {{app}} database check

# Read-only safety gate before downgrading the global model/router revisions.
migration-global-downgrade-preflight:
    uv run python scripts/preflight_global_resource_downgrade.py

# ── Run ──────────────────────────────────────────────────────────────────────

# Run the complete Docker development stack. The first invocation creates a
# gitignored .env.docker-dev with random secrets. Python reload and Vite HMR are
# enabled; dependency/lockfile changes require a rebuild.
dev:
    ./scripts/dev-compose.sh

# Stop the development stack while preserving database and dependency volumes.
dev-down:
    ./scripts/dev-compose.sh down

# Run the dev server with auto-reload.
run:
    uv run litestar --app {{app}} run --reload

# Serve like production (uvicorn, honoring proxy headers). Trusts X-Forwarded-For/-Proto
# only from FORWARDED_ALLOW_IPS (defaults to loopback); export it to the reverse proxy's
# IP/CIDR when deploying outside Docker (see docker-entrypoint.sh).
serve:
    uv run uvicorn {{app}} --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"

# ── Documentation ─────────────────────────────────────────────────────────────

# Prepare the MkDocs source projection without duplicating canonical docs.
docs-prepare:
    mkdir -p .mkdocs-docs
    ln -sfn ../README.md .mkdocs-docs/index.md
    ln -sfn ../EXAMPLES.md .mkdocs-docs/EXAMPLES.md
    ln -sfn ../CONTRIBUTING.md .mkdocs-docs/CONTRIBUTING.md
    ln -sfn ../SECURITY.md .mkdocs-docs/SECURITY.md
    ln -sfn ../docs .mkdocs-docs/docs
    ln -sfn ../issues .mkdocs-docs/issues
    ln -sfn ../plans .mkdocs-docs/plans

# Build the MkDocs documentation site.
docs-build: docs-prepare
    uv run mkdocs build --strict

# Serve the MkDocs documentation site locally.
docs-serve: docs-prepare
    uv run mkdocs serve

# ── Admin UI (ui/) ──────────────────────────────────────────────────────────────
# React/Vite admin console (Plan 03). Served under `/ui/`; the gateway API stays
# at the root. Requires Node + pnpm (https://pnpm.io). All recipes run in ui/.

# Install UI dependencies exactly as locked.
ui-install:
    cd ui && pnpm install --frozen-lockfile

# Run the Vite dev server (http://localhost:5173/ui/). Proxies every non-`/ui`
# path to the gateway — set GATEWAY_URL to point elsewhere (default :8000).
# Start the gateway first with `just run`.
ui-dev:
    cd ui && pnpm dev

# Production build (type-checks, then `vite build` → ui/dist).
ui-build:
    cd ui && pnpm build

# Regenerate the typed API client from the OpenAPI schema. By default it reads
# the checked-in ui/openapi.json; refresh that file from a running gateway with:
#   uv run litestar --app {{app}} schema openapi --output ui/openapi.json
# (or `curl -s localhost:8000/openapi.json -o ui/openapi.json`) before running this.
ui-typegen:
    cd ui && pnpm typegen

# Regenerate ui/openapi.json from the app, then the typed client. No server needed.
ui-schema:
    uv run litestar --app {{app}} schema openapi --output ui/openapi.json
    cd ui && pnpm typegen

# Lint the UI sources (eslint).
ui-lint:
    cd ui && pnpm lint

# ── Aggregates ─────────────────────────────────────────────────────────────────

# Run the full CI gate locally: hooks (lint/format/secrets) + types + tests.
check: pre-commit typecheck test

# Auto-fix everything fixable, then format.
fix: lint-fix format
