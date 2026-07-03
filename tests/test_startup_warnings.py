"""Operational misconfigurations must be loud at startup, not silent.

Without REDIS_URL the rate-limit stores are in-memory *per process*: with N
workers/replicas every configured limit silently becomes N× the intended one.
A single production instance is legitimate, so this warns instead of failing —
but it must be impossible to miss in the logs.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

POSTGRES_URL = "postgresql+asyncpg://gateway:pw@db:5432/gateway"  # pragma: allowlist secret


def _settings(tmp_path: Path, **overrides) -> Settings:
    values: dict = {
        "database_url": POSTGRES_URL,
        "admin_email": "admin@example.com",
        "master_key": "a-strong-random-master-key-0123456789",  # pragma: allowlist secret
        "jwt_secret": "a-strong-random-jwt-secret-0123456789",  # pragma: allowlist secret
        "salt_key": "a-strong-random-salt-key-0123456789",  # pragma: allowlist secret
        "environment": "production",
    }
    values.update(overrides)
    return Settings(**values)


def test_production_without_redis_warns_about_per_process_rate_limits(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        create_app(_settings(tmp_path))
    assert any("REDIS_URL" in record.message for record in caplog.records)


def test_production_with_redis_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        create_app(_settings(tmp_path, redis_url="redis://localhost:6379"))
    assert not any("REDIS_URL" in record.message for record in caplog.records)


def test_development_does_not_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings(
        tmp_path,
        environment="development",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'w.db'}",
    )
    with caplog.at_level(logging.WARNING):
        create_app(settings)
    assert not any("REDIS_URL" in record.message for record in caplog.records)
