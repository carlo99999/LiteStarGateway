# Design doc — Production Postgres

> **Status:** Implemented — `asyncpg` is a dependency (`pyproject.toml`), pool
> knobs live in `Settings` (`config.py`), and `docker-compose.yml` runs the app
> against `postgres:17`. Retained as the original design rationale.

## 1. Goal

Run and validate the gateway on **PostgreSQL** (`postgresql+asyncpg://…`), not
just dev SQLite. SQLite is single-writer with weak concurrency; a real gateway
needs Postgres. The code is already SQLAlchemy/Advanced Alchemy, so this is mostly
validation + config, not a rewrite.

## 2. Plan

- **Driver**: `asyncpg` (shipped in deps — `pyproject.toml`). Connection string
  via `DATABASE_URL` (already supported).
- **Connection pool**: pool knobs are exposed via `Settings` — `db_pool_size` and
  `db_max_overflow` (`config.py`), read from `DB_POOL_SIZE` / `DB_MAX_OVERFLOW`.
- **Validate concurrency behavior**: the unit-of-work flows (`create_team`,
  `register`) and the atomic invite `UPDATE` must be exercised on Postgres. In
  particular re-confirm the unit-of-work commit path here (SQLite masked an
  earlier autocommit issue).
- **Datetime/UTC**: confirm timezone handling on Postgres (the `_as_utc` coercion
  for naive SQLite datetimes should be a no-op on `timestamptz`).
- **Run the test suite against Postgres** in addition to SQLite (see CI doc:
  `services: postgres`).

## 3. Interaction with migrations

`create_all` stays for dev/test; production uses Alembic migrations
(`adding-db-migrations`). The Postgres baseline schema comes from those
migrations, not `create_all`.

## 4. Open decisions

1. **Pool sizing defaults** (per worker) — start conservative, tune under load.
2. **SSL/`sslmode`** for managed Postgres — surface via the URL/params.
3. **Load test** target (RPS, concurrency) to validate pool + provider resilience
   together before launch.
4. **JSON columns**: `params`/`encrypted_values` map to `JSON`/`JSONB` — confirm
   `JSONB` is used where filtering might matter.

## 5. Testing

- Parametrize the integration fixtures to also run on a Postgres service in CI.
- A concurrency test for the invite single-use `UPDATE` and `create_team`
  unit-of-work on Postgres.

## 6. Rollout

1. `feat/postgres-support` — asyncpg dep + pool config in `Settings`/`database.py`
   - README.
2. `feat/postgres-ci` — CI matrix/service running the suite on Postgres.
