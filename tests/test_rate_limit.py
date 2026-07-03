"""Tests for rate limiting on auth endpoints + the inference key function."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from litestar.status_codes import (
    HTTP_401_UNAUTHORIZED,
    HTTP_429_TOO_MANY_REQUESTS,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.web.rate_limit import (
    AUTH_RATE_LIMIT,
    _inference_identifier,
)


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'rl.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


async def test_login_is_rate_limited_per_ip(client: AsyncTestClient) -> None:
    limit = AUTH_RATE_LIMIT[1]
    payload = {"email": "ghost@b.com", "password": "nope"}

    # The budget's worth of (failing) attempts are allowed through to the handler.
    for _ in range(limit):
        resp = await client.post("/login", json=payload)
        assert resp.status_code == HTTP_401_UNAUTHORIZED

    # The next one is rejected by the limiter before reaching the handler.
    blocked = await client.post("/login", json=payload)
    assert blocked.status_code == HTTP_429_TOO_MANY_REQUESTS


class _FakeClient:
    host = "203.0.113.7"


def _request(authorization: str | None) -> Any:
    """Minimal Request stand-in exposing only what the identifier reads."""

    class _FakeRequest:
        def __init__(self) -> None:
            self.headers: dict[str, str] = (
                {"Authorization": authorization} if authorization is not None else {}
            )
            self.client = _FakeClient()

    return _FakeRequest()


def test_inference_identifier_keys_by_api_key() -> None:
    identifier = _inference_identifier(_request("Bearer lsk_secret-token"))
    assert identifier.startswith("key::")
    # The plaintext token must never appear in the cache key.
    assert "lsk_secret-token" not in identifier


def test_inference_identifier_falls_back_to_ip() -> None:
    assert _inference_identifier(_request(None)).startswith("ip::")
    assert _inference_identifier(_request("Bearer ")).startswith("ip::")


def test_rate_limit_uses_memory_store_by_default(tmp_path: Path) -> None:
    from litestar.stores.memory import MemoryStore

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'rl.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",
        salt_key="test-salt-key",
    )
    app = create_app(settings)
    assert isinstance(app.stores.get("rate_limit_auth"), MemoryStore)


def test_rate_limit_uses_redis_store_when_configured(tmp_path: Path) -> None:
    # A configured REDIS_URL backs the limiter with a (lazily-connected) RedisStore,
    # so limits hold across replicas. No live Redis is needed to build the app.
    from litestar.stores.redis import RedisStore

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'rl.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",
        salt_key="test-salt-key",
        redis_url="redis://localhost:6379",
    )
    app = create_app(settings)
    assert isinstance(app.stores.get("rate_limit_auth"), RedisStore)
    assert isinstance(app.stores.get("rate_limit_inference"), RedisStore)
