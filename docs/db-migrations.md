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
   in the CI workflow. Plan 11 promotes this to a release gate: run the existing
   `just migration-check` command and fail on any diff.

## 5. Testing

- A test that runs `upgrade head` on a fresh temp DB and asserts the resulting
  schema matches the models (or that autogenerate yields no diff).
- Keep the existing test suite on `create_all` for speed; add one migration
  smoke test.

## 6. Rollout

1. `feat/alembic-setup` — Alembic env + baseline migration + README workflow +
   prod `create_all` gating.
2. `feat/migration-ci-check` — autogenerate-diff guard in CI.

## 7. Callable-alias registry rollout (`f52a1c9d0b34`)

This migration is deliberately **not rolling-deploy safe**. It takes a
PostgreSQL `SHARE` lock on `model`, `router`, `model_grant`, and `router_grant`
for a consistent collision preflight and backfill, but that lock ends when the
migration transaction commits. An old application pod could then write a
legacy row without its required `callable_alias` row.

Deploy this revision with an offline write freeze:

1. Stop or drain every old-version writer and disable jobs that mutate models,
   routers, or their grants.
2. Run `database upgrade` through `f52a1c9d0b34` while the freeze remains in
   force. Resolve any preflight collision and rerun before proceeding.
3. Roll out only application versions that write the alias registry.
4. Resume traffic and background writers after every old-version process has
   terminated and the new rollout is healthy.

Development auto-schema creation also fails fast when it detects a legacy
application schema without `callable_alias`; run `database upgrade` instead of
letting `create_all` create an empty registry that would hide legacy rows.

## 8. Immutable router revisions rollout (`a61d7e3c9b20`)

This migration also requires an offline write freeze. It resolves every router
dependency to a stable model ID, creates revision 1, and pins every existing
grant to that revision. Ambiguous or inaccessible dependencies abort before any
revision DDL is applied.

1. Drain writers that mutate routers, models, or grants.
2. Run `database upgrade` and remediate every preflight error before retrying.
3. Deploy only application instances that write immutable router revisions.
4. Resume writers after the new fleet is healthy.

Downgrade is intentionally blocked once any router has more than one revision,
or any grant is pinned away from the current head: the legacy schema cannot
represent that history without data loss. Development `create_all` likewise
refuses a registry-era schema that has not yet been upgraded to revisions.

## 9. Downgrading global resources

Revisions `90e784ecd46b`, `b213468f39d2`, and `c6366c44d858` change model and
router ownership so `team_id` may be NULL. Before downgrading through those
revisions, stop application writers, take a tested database backup, and run the
read-only safety gate against the same database:

```bash
just migration-global-downgrade-preflight
```

The command exits `0` only when every global resource can be restored without
guessing or deleting data. Promoted resources with a valid `origin_team_id` are
reassigned to that team automatically before the migration restores the NOT
NULL constraint. The preflight blocks when it finds any of these cases:

- a native global resource with no origin;
- an `origin_team_id` that no longer references an existing team;
- a resource with the same name already present in its origin team;
- a model at revision `90e784ecd46b`, where the provenance column did not yet
  exist.

For each blocker, choose an explicit owner or intentionally remove the resource.
Do not invent a placeholder team. On revisions containing `origin_team_id`, an
ownership repair has this shape; substitute reviewed resource and team IDs and
run it in an operator-controlled transaction:

```sql
UPDATE model
SET team_id = '<existing-team-id>', origin_team_id = '<existing-team-id>'
WHERE id = '<reviewed-model-id>' AND team_id IS NULL;

UPDATE router
SET team_id = '<existing-team-id>', origin_team_id = '<existing-team-id>'
WHERE id = '<reviewed-router-id>' AND team_id IS NULL;
```

At revision `90e784ecd46b`, set only `model.team_id` because
`origin_team_id` is not present. If the resource must not survive rollback,
delete it deliberately through the supported administration API, including any
dependent grants, rather than adding deletion to the migration.

Rerun the preflight until it reports `SAFE`, then downgrade to the reviewed
target and verify the applied revision:

```bash
just downgrade 79fc50bbd1a4
just migration-current
```

Keep writers stopped until post-downgrade checks finish. If a blocker is found
during the Alembic command despite the external preflight, the migration raises
before its schema DDL and prints this runbook location; remediate and retry.
