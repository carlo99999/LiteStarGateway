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

# ── Run ──────────────────────────────────────────────────────────────────────

# Run the dev server with auto-reload.
run:
    uv run litestar --app {{app}} run --reload

# Serve like production (uvicorn, honoring proxy headers).
serve:
    uv run uvicorn {{app}} --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"

# ── Aggregates ─────────────────────────────────────────────────────────────────

# Run the full CI gate locally: hooks (lint/format/secrets) + types + tests.
check: pre-commit typecheck test

# Auto-fix everything fixable, then format.
fix: lint-fix format
