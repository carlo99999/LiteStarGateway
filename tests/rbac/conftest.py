"""Shared fixtures/helpers for extended-RBAC tests (team roles + platform auditor)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from _invite_helpers import seed_team_and_invite
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client: AsyncTestClient, email: str, password: str) -> str:
    resp = await client.post("/login", json={"email": email, "password": password})
    assert resp.status_code == HTTP_200_OK, resp.text
    return resp.json()["access_token"]


async def _admin(client: AsyncTestClient) -> str:
    return await _login(client, ADMIN_EMAIL, MASTER_KEY)


async def _signup(client: AsyncTestClient, admin: str, email: str) -> str:
    """Invite + sign up a password user; returns the new user's id."""
    invite = await seed_team_and_invite(client, admin)
    resp = await client.post(
        "/signup",
        json={
            "invite_token": invite,
            "email": email,
            "password": "Sup3r-Secret!",  # pragma: allowlist secret
        },
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
    return resp.json()["id"]


async def _member_token(
    client: AsyncTestClient, admin: str, team_id: str, email: str, role: str
) -> str:
    """Provision `email`, add them to the team with `role`, return their JWT."""
    await _signup(client, admin, email)
    added = await client.post(
        f"/teams/{team_id}/members",
        json={"email": email, "role": role},
        headers=_bearer(admin),
    )
    assert added.status_code == HTTP_201_CREATED, added.text
    return await _login(client, email, "Sup3r-Secret!")


async def _team(client: AsyncTestClient, admin: str) -> str:
    org = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin))
    ).json()
    team = await client.post(
        f"/organizations/{org['id']}/teams",
        json={"name": "Core", "admin_email": ADMIN_EMAIL},
        headers=_bearer(admin),
    )
    return team.json()["id"]


async def _credential(client: AsyncTestClient, admin: str) -> str:
    resp = await client.post(
        "/credentials",
        json={"name": "cred-openai", "provider": "openai", "values": {"api_key": "x"}},
        headers=_bearer(admin),
    )
    return resp.json()["id"]


def _model_payload(cred: str, name: str = "fast-chat") -> dict:
    return {
        "name": name,
        "provider": "openai",
        "credential_id": cred,
        "type": "chat",
        "provider_model_id": "gpt-4o",
    }
