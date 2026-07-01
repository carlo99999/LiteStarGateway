"""Unit tests for the environment-aware logging factory."""

from __future__ import annotations

from litestar.logging import LoggingConfig, StructLoggingConfig

from litestar_test.config import Settings
from litestar_test.infrastructure.logging import build_logging_config


def _settings(environment: str) -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        admin_email="admin@example.com",
        master_key="m",
        jwt_secret="x" * 40,
        salt_key="s" * 32,
        environment=environment,
    )


def test_development_uses_stdlib_console_logging() -> None:
    assert isinstance(build_logging_config(_settings("development")), LoggingConfig)


def test_production_uses_structured_json_logging() -> None:
    assert isinstance(build_logging_config(_settings("production")), StructLoggingConfig)
