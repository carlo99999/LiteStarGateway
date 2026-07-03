"""Builds the Advanced Alchemy config/plugin — the single source of truth.

The same `config` instance is shared between the app and the auth middleware so
that app-state keys (which AA suffixes when multiple configs exist) always match.
"""

from __future__ import annotations

from dataclasses import dataclass

from advanced_alchemy.extensions.litestar import (
    EngineConfig,
    SQLAlchemyAsyncConfig,
    SQLAlchemyPlugin,
)

from litestar_test.config import Settings

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


@dataclass(frozen=True)
class Database:
    config: SQLAlchemyAsyncConfig
    plugin: SQLAlchemyPlugin


def create_database(settings: Settings) -> Database:
    # Dev/test auto-create the schema for zero-config runs; production uses Alembic
    # migrations instead (the container runs `database upgrade` on start), so the
    # two never fight over creating the same tables.
    config = SQLAlchemyAsyncConfig(
        connection_string=settings.database_url,
        create_all=not settings.is_production,
        engine_config=_engine_config(settings),
    )
    return Database(config=config, plugin=SQLAlchemyPlugin(config=config))
