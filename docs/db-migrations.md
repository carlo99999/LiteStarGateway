# Design doc — Database migrations (Alembic)

> **Status:** Implemented — Alembic env (`migrations/env.py`, `alembic.ini`) with a
> baseline plus per-change revisions in `migrations/versions/`; `create_all` is off
> in production (schema managed by migrations, run on start). Retained as the
> original design rationale.

## 1. Goal

Today the schema is created with `create_all=True` (in
`infrastructure/persistence/database.py`). That is fine for dev/SQLite but unsafe
in production: it never alters existing tables, so any change to `orm.py` (new
column, new table, constraint) silently fails to apply to a populated database.
We need **versioned, reviewable migrations**.

## 2. Tooling: Alembic via Advanced Alchemy

We already use Advanced Alchemy, which ships Alembic integration and a CLI. Reuse
it rather than wiring raw Alembic:

- Alembic targets our existing declarative metadata (`base.UUIDAuditBase.metadata`
  from `orm.py`) as `target_metadata` for autogenerate.
- The Litestar Advanced Alchemy plugin exposes DB commands (init/migrate/upgrade)
  consistent with our `SQLAlchemyAsyncConfig`.

## 3. Plan

1. Add an Alembic env wired to `target_metadata = base.UUIDAuditBase.metadata` and
   to `Settings.database_url` (async engine: `asyncpg`/`aiosqlite`).
2. Generate the **baseline** migration from the current models (the schema
   `create_all` produces today) so existing dev DBs converge.
3. **Turn off `create_all` in production**; keep it allowed only in
   dev/test (e.g. gated by `ENVIRONMENT` or a `Settings.auto_create_all` flag
   defaulting to dev-on / prod-off). Tests can keep `create_all` for speed.
4. Document the workflow in the README: `alembic revision --autogenerate`,
   review, `alembic upgrade head`; run `upgrade head` on deploy.

## 4. Decisions (as implemented)

1. **create_all coexistence**: gated by environment — `create_all` for dev/test,
   disabled in production (production uses Alembic migrations only, run on start).
2. **Async vs sync Alembic env**: async env, using the async template
   (`run_async`/`run_migrations_online` with the async engine) to match
   `asyncpg`/`aiosqlite`.
3. **Baseline strategy**: a single baseline from the current models, with
   `alembic stamp` for any existing deploy.
4. **SQLite limits**: SQLite's limited `ALTER TABLE` may require Alembic
   batch-mode for some changes; Postgres is the production target anyway.

Still open:

4. **CI check**: add a test/CI step asserting "no pending autogenerate diff"
   (models and migrations are in sync), to catch forgotten migrations — not yet
   in the CI workflow.

## 5. Testing

- A test that runs `upgrade head` on a fresh temp DB and asserts the resulting
  schema matches the models (or that autogenerate yields no diff).
- Keep the existing test suite on `create_all` for speed; add one migration
  smoke test.

## 6. Rollout

1. `feat/alembic-setup` — Alembic env + baseline migration + README workflow +
   prod `create_all` gating.
2. `feat/migration-ci-check` — autogenerate-diff guard in CI.
