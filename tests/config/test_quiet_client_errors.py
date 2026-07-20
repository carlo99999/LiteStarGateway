"""Routine client errors must not spam ERROR tracebacks.

Browsers and scanners probe unknown paths constantly (e.g. Chrome DevTools'
`/.well-known/appspecific/com.chrome.devtools.json`); each hit was logged as an
ERROR with a full traceback. The 404/405 response and its access-log line are
unchanged — only the exception traceback is suppressed. Real errors (5xx) keep
their stack traces.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import HTTP_404_NOT_FOUND
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.logging import _QUIET_STATUSES, build_logging_config


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'quiet.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",  # pragma: allowlist secret
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


async def test_unknown_path_404_logs_no_error_traceback(
    client: AsyncTestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="litestar"):
        resp = await client.get("/.well-known/appspecific/com.chrome.devtools.json")
    assert resp.status_code == HTTP_404_NOT_FOUND
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


POSTGRES_URL = "postgresql+asyncpg://gateway:pw@db:5432/gateway"  # pragma: allowlist secret


def test_both_environments_suppress_404_and_405(tmp_path: Path) -> None:
    # The quiet set must be wired into dev (LoggingConfig) and production
    # (StructLoggingConfig) alike.
    for environment in ("development", "production"):
        settings = Settings(
            database_url=POSTGRES_URL,
            admin_email="admin@example.com",
            master_key="a-strong-random-master-key-0123456789",  # pragma: allowlist secret
            jwt_secret="a-strong-random-jwt-secret-0123456789",  # pragma: allowlist secret
            salt_key="a-strong-random-salt-key-0123456789",  # pragma: allowlist secret
            environment=environment,
            session_cookie_secure=True,
        )
        config = build_logging_config(settings)
        assert config.disable_stack_trace == _QUIET_STATUSES, environment
