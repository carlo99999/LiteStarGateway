"""Unit tests for connection-pool configuration selection."""

from __future__ import annotations

from litestar_gateway.config import (
    DEFAULT_DB_MAX_OVERFLOW,
    DEFAULT_DB_POOL_SIZE,
    Settings,
)
from litestar_gateway.infrastructure.persistence.database import create_database


def _settings(url: str) -> Settings:
    return Settings(
        database_url=url,
        admin_email="admin@example.com",
        master_key="m" * 32,
        jwt_secret="x" * 40,
        salt_key="s" * 32,
    )


def test_postgres_gets_a_sized_pool() -> None:
    engine_config = create_database(
        _settings("postgresql+asyncpg://u:p@h:5432/db")
    ).config.engine_config
    assert engine_config.pool_size == DEFAULT_DB_POOL_SIZE
    assert engine_config.max_overflow == DEFAULT_DB_MAX_OVERFLOW
    assert engine_config.pool_pre_ping is True


def test_sqlite_leaves_pool_at_library_defaults() -> None:
    # SQLite/aiosqlite has no sized pool; we must not force pool sizing on it.
    engine_config = create_database(_settings("sqlite+aiosqlite:///:memory:")).config.engine_config
    assert engine_config.pool_pre_ping is not True
    assert engine_config.pool_size != DEFAULT_DB_POOL_SIZE


def test_create_all_enabled_in_development() -> None:
    # Dev/test auto-create the schema (no migrations needed).
    assert create_database(_settings("sqlite+aiosqlite:///:memory:")).config.create_all is True


def test_create_all_disabled_in_production() -> None:
    # Production manages the schema via Alembic migrations, not create_all.
    production = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        admin_email="admin@example.com",
        master_key="m" * 32,
        jwt_secret="x" * 40,
        salt_key="s" * 32,
        environment="production",
    )
    assert create_database(production).config.create_all is False
