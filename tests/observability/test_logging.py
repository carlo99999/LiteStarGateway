"""Unit tests for the environment-aware logging factory."""

from __future__ import annotations

from litestar.logging import LoggingConfig, StructLoggingConfig

from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.logging import build_logging_config


def _settings(environment: str) -> Settings:
    # Postgres URL: production settings refuse SQLite (no connection is opened).
    return Settings(
        database_url="postgresql+asyncpg://gateway:pw@db:5432/gateway",  # pragma: allowlist secret
        admin_email="admin@example.com",
        master_key="m" * 32,
        jwt_secret="x" * 40,
        salt_key="s" * 32,
        environment=environment,
        session_cookie_secure=True,
    )


def test_development_uses_stdlib_console_logging() -> None:
    assert isinstance(build_logging_config(_settings("development")), LoggingConfig)


def test_production_uses_structured_json_logging() -> None:
    assert isinstance(build_logging_config(_settings("production")), StructLoggingConfig)
