"""Builds the Advanced Alchemy config/plugin — the single source of truth.

The same `config` instance is shared between the app and the auth middleware so
that app-state keys (which AA suffixes when multiple configs exist) always match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from advanced_alchemy.extensions.litestar import (
    EngineConfig,
    SQLAlchemyAsyncConfig,
    SQLAlchemyPlugin,
)
from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from litestar_gateway.config import Settings

# Recycle pooled connections periodically to avoid stale ones (idle timeouts,
# failovers). Postgres only — SQLite/aiosqlite does not use a sized pool.
_POOL_RECYCLE_SECONDS = 1800


def _engine_config(settings: Settings) -> EngineConfig:
    if settings.is_postgres:
        return EngineConfig(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            pool_recycle=_POOL_RECYCLE_SECONDS,
        )
    return EngineConfig()


def _create_engine_with_sqlite_fk(connection_string: str, **kwargs: Any) -> AsyncEngine:
    # Wraps the default engine factory so the FK pragma listener is attached at
    # engine-creation time (during app startup), not when the config is built.
    # Attaching earlier would populate `config.engine_instance` before Litestar
    # deep-copies the route handlers, and the engine holds a non-copyable module
    # reference. aiosqlite (dev/test) does not enforce foreign keys unless asked
    # per connection, so without this a delete can silently orphan a child row —
    # diverging from Postgres (prod), which always enforces them. No-op for
    # non-SQLite dialects (Postgres ignores it entirely).
    engine = create_async_engine(connection_string, **kwargs)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


@dataclass(frozen=True)
class Database:
    config: SQLAlchemyAsyncConfig
    plugin: SQLAlchemyPlugin


_APPLICATION_SCHEMA_ANCHORS = frozenset({"user", "team", "model", "router"})


class GuardedSQLAlchemyAsyncConfig(SQLAlchemyAsyncConfig):
    """Prevent dev ``create_all`` from masking an unmigrated legacy schema."""

    async def create_all_metadata(self, app: Any) -> None:
        async with self.get_engine().connect() as connection:
            tables = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
        if tables & _APPLICATION_SCHEMA_ANCHORS and "callable_alias" not in tables:
            raise RuntimeError(
                "Legacy application schema detected without callable_alias; "
                "refusing create_all because it would create an empty registry. "
                "Run `database upgrade` before starting the application."
            )
        await super().create_all_metadata(app)


def create_database(settings: Settings) -> Database:
    # Auto-create the schema for zero-config dev/test runs; production (and the
    # migration-managed dev container, via AUTO_CREATE_SCHEMA=false) uses Alembic
    # instead, so create_all and `database upgrade` never fight over creating the
    # same tables ("relation already exists").
    config = GuardedSQLAlchemyAsyncConfig(
        connection_string=settings.database_url,
        create_all=settings.should_create_schema,
        engine_config=_engine_config(settings),
        # Attach the SQLite FK pragma when the plugin lazily creates the engine.
        create_engine_callable=_create_engine_with_sqlite_fk,
    )
    return Database(config=config, plugin=SQLAlchemyPlugin(config=config))
