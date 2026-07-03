"""Regression tests: API-key and JWT auth realms are not interchangeable.

Model calls (`/v1/*`) accept only an issued `lsk_` API key; the management
endpoints (`/me`, ...) accept only a login JWT. A credential valid in one realm
must be rejected (401) in the other.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import HTTP_401_UNAUTHORIZED
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'realm.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin_jwt(client: AsyncTestClient) -> str:
    resp = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    return resp.json()["access_token"]


async def _issue_api_key(client: AsyncTestClient, admin_jwt: str) -> str:
    org = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin_jwt))
    ).json()["id"]
    team = (
        await client.post(
            f"/organizations/{org}/teams",
            json={"name": "Core", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin_jwt),
        )
    ).json()["id"]
    return (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin_jwt))
    ).json()["plaintext"]


async def test_jwt_is_rejected_on_inference_route(client: AsyncTestClient) -> None:
    # A genuine, well-signed login JWT must NOT authenticate a model call: the
    # API-key middleware hashes the bearer and finds no matching key.
    jwt = await _admin_jwt(client)
    resp = await client.get("/whoami", headers=_bearer(jwt))
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_api_key_is_rejected_on_management_route(client: AsyncTestClient) -> None:
    # A real issued API key must NOT authenticate a JWT-protected route.
    jwt = await _admin_jwt(client)
    api_key = await _issue_api_key(client, jwt)
    assert api_key.startswith("lsk_")
    resp = await client.get("/me", headers=_bearer(api_key))
    assert resp.status_code == HTTP_401_UNAUTHORIZED
