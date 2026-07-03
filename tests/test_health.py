"""Liveness and readiness probes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'health.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


async def test_liveness_is_ok(client: AsyncTestClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == HTTP_200_OK
    assert resp.json() == {"status": "ok"}


async def test_readiness_ok_when_db_reachable(client: AsyncTestClient) -> None:
    resp = await client.get("/health/ready")
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"


async def test_readiness_503_when_db_unavailable(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    async def boom(self: object, *args: object, **kwargs: object) -> object:
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(AsyncSession, "execute", boom)
    resp = await client.get("/health/ready")
    assert resp.status_code == HTTP_503_SERVICE_UNAVAILABLE
    assert resp.json()["checks"]["database"] == "down"
