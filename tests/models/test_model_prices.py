"""Default per-token pricing lookup (console prefill).

The catalog is a bundled snapshot, so these assert the wiring — an exact hit,
a miss (404), and that the endpoint is behind auth — not the exact prices,
which drift as the snapshot is refreshed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import Provider
from litestar_gateway.infrastructure.pricing import ModelPriceCatalog

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"
SALT_KEY = "unit-test-salt-key"


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=SALT_KEY,
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


async def _admin_token(client: AsyncTestClient) -> str:
    resp = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    return resp.json()["access_token"]


def test_catalog_hit_and_miss() -> None:
    catalog = ModelPriceCatalog()
    # gpt-4o is a stable flagship id present in the snapshot for both providers.
    hit = catalog.lookup(Provider.OPENAI, "gpt-4o")
    assert hit is not None
    assert hit.input_cost_per_token > 0
    assert hit.output_cost_per_token > 0
    # An id that will never exist yields None (form leaves the fields blank).
    assert catalog.lookup(Provider.OPENAI, "no-such-model-xyz") is None


async def test_price_endpoint_returns_costs_for_known_model(client: AsyncTestClient) -> None:
    token = await _admin_token(client)
    resp = await client.get(
        "/model-prices",
        params={"provider": "openai", "provider_model_id": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    assert body["provider"] == "openai"
    assert body["provider_model_id"] == "gpt-4o"
    assert body["input_cost_per_token"] > 0
    assert body["output_cost_per_token"] > 0


async def test_price_endpoint_404_for_unknown_model(client: AsyncTestClient) -> None:
    token = await _admin_token(client)
    resp = await client.get(
        "/model-prices",
        params={"provider": "openai", "provider_model_id": "no-such-model-xyz"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == HTTP_404_NOT_FOUND


async def test_price_endpoint_requires_auth(client: AsyncTestClient) -> None:
    resp = await client.get(
        "/model-prices",
        params={"provider": "openai", "provider_model_id": "gpt-4o"},
    )
    assert resp.status_code == HTTP_401_UNAUTHORIZED
