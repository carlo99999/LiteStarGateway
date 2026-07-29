"""Plan 18 Phase 2 — declaring capabilities through the models API."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED, HTTP_400_BAD_REQUEST
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"  # pragma: allowlist secret
SALT_KEY = "unit-test-salt-key"  # pragma: allowlist secret


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=SALT_KEY,
        openai_compatible_allowed_hosts=("10.42.0.0/16",),
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


async def _headers(client: AsyncTestClient) -> dict[str, str]:
    token = (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _credential(client: AsyncTestClient, provider: str, name: str) -> str:
    values = (
        {"api_base": "http://10.42.0.9:8000/v1"}
        if provider == "openai_compatible"
        else {"api_key": "sk-test"}  # pragma: allowlist secret
    )
    response = await client.post(
        "/credentials",
        json={"name": name, "provider": provider, "values": values},
        headers=await _headers(client),
    )
    assert response.status_code == HTTP_201_CREATED, response.text
    return response.json()["id"]


async def _create_model(
    client: AsyncTestClient,
    credential_id: str,
    provider: str,
    capabilities: list[str] | None,
    name: str,
):
    body: dict[str, object] = {
        "name": name,
        "provider": provider,
        "credential_id": credential_id,
        "type": "chat",
        "provider_model_id": "upstream",
    }
    if capabilities is not None:
        body["capabilities"] = capabilities
    return await client.post("/platform/models", json=body, headers=await _headers(client))


async def test_a_model_defaults_to_chat_only(client: AsyncTestClient) -> None:
    credential_id = await _credential(client, "openai_compatible", "vllm")
    response = await _create_model(client, credential_id, "openai_compatible", None, "m1")
    assert response.status_code == HTTP_201_CREATED, response.text
    assert response.json()["capabilities"] == ["chat.completions"]


async def test_capabilities_round_trip(client: AsyncTestClient) -> None:
    credential_id = await _credential(client, "openai_compatible", "vllm")
    response = await _create_model(
        client,
        credential_id,
        "openai_compatible",
        ["chat.completions", "embeddings"],
        "m2",
    )
    assert response.status_code == HTTP_201_CREATED, response.text
    # Sorted on the way out, so the response is stable regardless of set order.
    assert response.json()["capabilities"] == ["chat.completions", "embeddings"]


async def test_an_unknown_capability_is_refused(client: AsyncTestClient) -> None:
    credential_id = await _credential(client, "openai_compatible", "vllm")
    response = await _create_model(client, credential_id, "openai_compatible", ["telepathy"], "m3")
    assert response.status_code == HTTP_400_BAD_REQUEST
    assert "telepathy" in response.text


async def test_declaring_on_a_fixed_provider_is_refused(client: AsyncTestClient) -> None:
    # Silently ignoring it would leave an operator believing they had
    # constrained an OpenAI model they had not.
    credential_id = await _credential(client, "openai", "oai")
    response = await _create_model(
        client, credential_id, "openai", ["chat.completions", "embeddings"], "m4"
    )
    assert response.status_code == HTTP_400_BAD_REQUEST


async def test_update_can_widen_a_declaration(client: AsyncTestClient) -> None:
    credential_id = await _credential(client, "openai_compatible", "vllm")
    created = await _create_model(client, credential_id, "openai_compatible", None, "m5")
    model_id = created.json()["id"]

    updated = await client.patch(
        f"/platform/models/{model_id}",
        json={"capabilities": ["chat.completions", "embeddings"]},
        headers=await _headers(client),
    )
    assert updated.status_code == HTTP_200_OK, updated.text
    assert updated.json()["capabilities"] == ["chat.completions", "embeddings"]


async def test_update_rejects_an_unknown_capability(client: AsyncTestClient) -> None:
    credential_id = await _credential(client, "openai_compatible", "vllm")
    created = await _create_model(client, credential_id, "openai_compatible", None, "m6")
    model_id = created.json()["id"]

    updated = await client.patch(
        f"/platform/models/{model_id}",
        json={"capabilities": ["responses"]},
        headers=await _headers(client),
    )
    assert updated.status_code == HTTP_400_BAD_REQUEST
