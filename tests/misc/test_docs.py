"""The OpenAPI docs are served at the root path (the landing page)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_401_UNAUTHORIZED, HTTP_404_NOT_FOUND
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'docs.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


async def test_docs_served_at_root(client: AsyncTestClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == HTTP_200_OK
    assert "text/html" in resp.headers.get("content-type", "")


async def test_openapi_schema_at_root(client: AsyncTestClient) -> None:
    resp = await client.get("/openapi.json")
    assert resp.status_code == HTTP_200_OK
    assert resp.json()["info"]["title"] == "Litestar Gateway API"


async def test_docs_can_be_disabled(tmp_path: Path) -> None:
    # With OPENAPI_ENABLED off, the docs + schema are not served (production hardening).
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'nodocs.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",
        salt_key="test-salt-key",
        openapi_enabled=False,
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        assert (await test_client.get("/openapi.json")).status_code == HTTP_404_NOT_FOUND


async def test_docs_do_not_shadow_protected_routes(client: AsyncTestClient) -> None:
    # Serving docs at "/" must not open the API group: /whoami still needs a key.
    assert (await client.get("/whoami")).status_code == HTTP_401_UNAUTHORIZED
