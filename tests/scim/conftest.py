"""Shared fixtures/helpers for SCIM 2.0 provisioning tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from _invite_helpers import seed_team_and_invite
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"

ERROR_URN = "urn:ietf:params:scim:api:messages:2.0:Error"
LIST_URN = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
USER_URN = "urn:ietf:params:scim:schemas:core:2.0:User"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'scim.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    app = create_app(_settings(tmp_path))
    async with AsyncTestClient(app=app) as test_client:
        yield test_client


async def _admin_headers(client: AsyncTestClient) -> dict[str, str]:
    resp = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    assert resp.status_code == HTTP_200_OK, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _mint_token(client: AsyncTestClient, name: str = "entra") -> str:
    resp = await client.post(
        "/scim-tokens", json={"name": name}, headers=await _admin_headers(client)
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
    return resp.json()["token"]


async def _scim_headers(client: AsyncTestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {await _mint_token(client)}"}


async def _create_user(
    client: AsyncTestClient,
    headers: dict[str, str],
    email: str,
    external_id: str | None = "ext-1",
) -> dict:
    body: dict = {"schemas": [USER_URN], "userName": email, "active": True}
    if external_id is not None:
        body["externalId"] = external_id
    resp = await client.post("/scim/v2/Users", json=body, headers=headers)
    assert resp.status_code == HTTP_201_CREATED, resp.text
    return resp.json()


async def _signup_password_user(client: AsyncTestClient, email: str, password: str) -> None:
    headers = await _admin_headers(client)
    admin_token = headers["Authorization"].removeprefix("Bearer ")
    invite_token = await seed_team_and_invite(client, admin_token)
    resp = await client.post(
        "/signup",
        json={"invite_token": invite_token, "email": email, "password": password},
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
