"""Builds the Advanced Alchemy config/plugin — the single source of truth.

The same `config` instance is shared between the app and the auth middleware so
that app-state keys (which AA suffixes when multiple configs exist) always match.
"""

from __future__ import annotations

from dataclasses import dataclass

from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig, SQLAlchemyPlugin

from litestar_test.config import Settings


@dataclass(frozen=True)
class Database:
    config: SQLAlchemyAsyncConfig
    plugin: SQLAlchemyPlugin


def create_database(settings: Settings) -> Database:
    # create_all=True is fine for SQLite/dev; use Alembic migrations in production.
    config = SQLAlchemyAsyncConfig(
        connection_string=settings.database_url,
        create_all=True,
    )
    return Database(config=config, plugin=SQLAlchemyPlugin(config=config))
