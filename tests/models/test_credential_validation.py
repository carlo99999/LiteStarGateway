"""Creation-time validation of credential `values` per provider."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import HTTP_201_CREATED, HTTP_400_BAD_REQUEST
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"
SALT_KEY = "unit-test-salt-key"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'cred.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=SALT_KEY,
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin_token(client: AsyncTestClient) -> str:
    return (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]


async def _create(client: AsyncTestClient, name: str, provider: str, values: dict[str, str]) -> int:
    admin = await _admin_token(client)
    resp = await client.post(
        "/credentials",
        json={"name": name, "provider": provider, "values": values},
        headers=_bearer(admin),
    )
    return resp.status_code


VALID = [
    ("openai", {"api_key": "sk-x"}),
    ("openai", {"api_key": "sk-x", "api_base": "https://proxy", "organization": "org-1"}),
    ("anthropic", {"api_key": "sk-ant-x", "api_base": "https://proxy"}),
    (
        "azure_openai",
        {"api_key": "az", "api_base": "https://a.openai.azure.com", "api_version": "2024-02-15"},
    ),
    # vertex_credentials is optional: the adapter falls back to ADC.
    ("vertex_ai", {"vertex_project": "p", "vertex_location": "us-central1"}),
    (
        "vertex_ai",
        {"vertex_project": "p", "vertex_location": "us-central1", "vertex_credentials": "{}"},
    ),
    ("databricks", {"api_key": "dapi-x", "api_base": "https://w.databricks.com"}),
]


@pytest.mark.parametrize(("provider", "values"), VALID)
async def test_valid_values_accepted(
    client: AsyncTestClient, provider: str, values: dict[str, str]
) -> None:
    assert await _create(client, f"ok-{provider}", provider, values) == HTTP_201_CREATED


INVALID = [
    ("openai", {}),  # missing api_key
    ("openai", {"api_base": "https://proxy"}),  # missing api_key
    ("openai", {"api_key": ""}),  # blank required value
    ("openai", {"api_key": "sk-x", "api_keys": "typo"}),  # unexpected key
    ("anthropic", {"organization": "org-1", "api_key": "x"}),  # key not valid for anthropic
    ("azure_openai", {"api_key": "az", "api_base": "https://a"}),  # missing api_version
    ("vertex_ai", {"vertex_project": "p"}),  # missing vertex_location
    ("vertex_ai", {"api_key": "sk-x"}),  # openai-shaped values on vertex
    ("databricks", {"api_key": "dapi-x"}),  # missing api_base
]


@pytest.mark.parametrize(("provider", "values"), INVALID)
async def test_invalid_values_rejected_with_400(
    client: AsyncTestClient, provider: str, values: dict[str, str]
) -> None:
    assert await _create(client, f"bad-{provider}", provider, values) == HTTP_400_BAD_REQUEST


async def test_error_names_the_offending_keys(client: AsyncTestClient) -> None:
    admin = await _admin_token(client)
    resp = await client.post(
        "/credentials",
        json={"name": "bad", "provider": "azure_openai", "values": {"api_key": "az"}},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST
    detail = resp.json()["detail"]
    assert "api_base" in detail
    assert "api_version" in detail


async def test_provider_without_rules_is_accepted(client: AsyncTestClient) -> None:
    # bedrock has no field rules yet: accept as before.
    status = await _create(client, "bedrock-any", "bedrock", {"anything": "goes"})
    assert status == HTTP_201_CREATED
