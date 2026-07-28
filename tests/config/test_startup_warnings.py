"""Operational misconfigurations must be loud at startup, not silent.

Without REDIS_URL every shared component — rate limits, circuit breaker,
response cache — falls back to per-process state: with N workers/replicas each
limit silently becomes N× the intended one, and the breaker's state machine
diverges per replica (which is how ISSUE-029 happened). In **production** that
is now a startup failure, treated like the PostgreSQL requirement: an
infrastructural dependency, not a preference. Other non-local environments keep
the warning.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from litestar_gateway.app import create_app
from litestar_gateway.config import InsecureConfigurationError, Settings

POSTGRES_URL = "postgresql+asyncpg://gateway:pw@db:5432/gateway"  # pragma: allowlist secret


def _settings(tmp_path: Path, **overrides) -> Settings:
    values: dict = {
        "database_url": POSTGRES_URL,
        "admin_email": "admin@example.com",
        "master_key": "a-strong-random-master-key-0123456789",  # pragma: allowlist secret
        "jwt_secret": "a-strong-random-jwt-secret-0123456789",  # pragma: allowlist secret
        "salt_key": "a-strong-random-salt-key-0123456789",  # pragma: allowlist secret
        "environment": "production",
        "session_cookie_secure": True,
        "redis_url": "redis://localhost:6379",
    }
    values.update(overrides)
    return Settings(**values)


def test_production_without_redis_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(InsecureConfigurationError, match="REDIS_URL"):
        _settings(tmp_path, redis_url=None)


def test_a_deployed_non_production_environment_still_only_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Staging on one replica is a legitimate setup, and demanding Redis there
    would make the environment harder to stand up than production is to run.
    The warning is what keeps it from being silent."""
    settings = _settings(tmp_path, environment="staging", redis_url=None)
    with caplog.at_level(logging.WARNING):
        create_app(settings)
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
        redis_url=None,
    )
    with caplog.at_level(logging.WARNING):
        create_app(settings)
    assert not any("REDIS_URL" in record.message for record in caplog.records)
