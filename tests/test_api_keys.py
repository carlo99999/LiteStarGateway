"""Integration tests for team-scoped API keys + API-key authentication."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'keys.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client: AsyncTestClient, email: str, password: str) -> str:
    resp = await client.post("/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


async def _setup_team(client: AsyncTestClient) -> tuple[str, str]:
    """Bootstrap admin → org → team (admin is team admin). Returns (token, team_id)."""
    token = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(token))
    ).json()["id"]
    team_id = (
        await client.post(
            f"/organizations/{org_id}/teams",
            json={"name": "Core", "admin_email": ADMIN_EMAIL},
            headers=_bearer(token),
        )
    ).json()["id"]
    return token, team_id


async def _create_key(client: AsyncTestClient, token: str, team_id: str) -> dict:
    resp = await client.post(f"/teams/{team_id}/keys", json={"name": "ci"}, headers=_bearer(token))
    assert resp.status_code == HTTP_201_CREATED
    return resp.json()


async def test_create_key_returns_plaintext_once(client: AsyncTestClient) -> None:
    token, team_id = await _setup_team(client)
    body = await _create_key(client, token, team_id)
    assert body["team_id"] == team_id
    assert body["plaintext"].startswith("lsk_")
    assert body["prefix"] == body["plaintext"][: len(body["prefix"])]


async def test_protected_route_rejects_without_key(client: AsyncTestClient) -> None:
    assert (await client.get("/whoami")).status_code == HTTP_401_UNAUTHORIZED


async def test_protected_route_rejects_invalid_key(client: AsyncTestClient) -> None:
    resp = await client.get("/whoami", headers=_bearer("lsk_nope"))
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_valid_key_grants_access_and_reports_team(client: AsyncTestClient) -> None:
    token, team_id = await _setup_team(client)
    key = (await _create_key(client, token, team_id))["plaintext"]
    resp = await client.get("/whoami", headers=_bearer(key))
    assert resp.status_code == HTTP_200_OK
    assert resp.json() == {"team_id": team_id}


async def test_revoked_key_is_rejected(client: AsyncTestClient) -> None:
    token, team_id = await _setup_team(client)
    created = await _create_key(client, token, team_id)
    key, key_id = created["plaintext"], created["id"]

    assert (await client.get("/whoami", headers=_bearer(key))).status_code == HTTP_200_OK

    revoke = await client.delete(f"/teams/{team_id}/keys/{key_id}", headers=_bearer(token))
    assert revoke.status_code in (200, 204)

    after = await client.get("/whoami", headers=_bearer(key))
    assert after.status_code == HTTP_401_UNAUTHORIZED


async def test_list_keys_hides_secret(client: AsyncTestClient) -> None:
    token, team_id = await _setup_team(client)
    await _create_key(client, token, team_id)
    resp = await client.get(f"/teams/{team_id}/keys", headers=_bearer(token))
    assert resp.status_code == HTTP_200_OK
    rows = resp.json()
    assert len(rows) == 1
    assert "plaintext" not in rows[0]
    assert "key_hash" not in rows[0]
    assert rows[0]["is_active"] is True


async def test_key_creation_requires_team_admin(client: AsyncTestClient) -> None:
    admin_token, team_id = await _setup_team(client)
    invite = (await client.post("/invites", headers=_bearer(admin_token))).json()["token"]
    await client.post(
        "/signup",
        json={"invite_token": invite, "email": "outsider@b.com", "password": "Passw0rd!"},
    )
    outsider = await _login(client, "outsider@b.com", "Passw0rd!")
    resp = await client.post(
        f"/teams/{team_id}/keys", json={"name": "x"}, headers=_bearer(outsider)
    )
    assert resp.status_code == HTTP_403_FORBIDDEN
