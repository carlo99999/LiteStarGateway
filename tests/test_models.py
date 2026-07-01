"""Integration tests for team-scoped model deployments."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_403_FORBIDDEN,
    HTTP_409_CONFLICT,
)
from litestar.testing import AsyncTestClient

from litestar_test.app import create_app
from litestar_test.config import Settings

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"
SALT_KEY = "unit-test-salt-key"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'models.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=SALT_KEY,
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client: AsyncTestClient, email: str, password: str) -> str:
    return (await client.post("/login", json={"email": email, "password": password})).json()[
        "access_token"
    ]


async def _credential(client: AsyncTestClient, admin: str, provider: str) -> str:
    return (
        await client.post(
            "/credentials",
            json={"name": f"cred-{provider}", "provider": provider, "values": {"api_key": "x"}},
            headers=_bearer(admin),
        )
    ).json()["id"]


async def _team(client: AsyncTestClient, admin: str) -> str:
    org_id = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin))
    ).json()["id"]
    return (
        await client.post(
            f"/organizations/{org_id}/teams",
            json={"name": "Core", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]


async def test_create_model(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    cred = await _credential(client, admin, "openai")
    team = await _team(client, admin)
    resp = await client.post(
        f"/teams/{team}/models",
        json={
            "name": "fast-chat",
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
            "params": {"temperature": 0.2},
            "input_cost_per_token": 0.000005,
        },
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_201_CREATED
    body = resp.json()
    assert body["provider"] == "openai"
    assert body["type"] == "chat"
    assert body["provider_model_id"] == "gpt-4o"
    assert body["params"] == {"temperature": 0.2}
    assert body["enabled"] is True


async def test_provider_must_match_credential(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    cred = await _credential(client, admin, "openai")  # OpenAI credential
    team = await _team(client, admin)
    resp = await client.post(
        f"/teams/{team}/models",
        json={
            "name": "mismatch",
            "provider": "anthropic",  # but model says Anthropic
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "claude-3-5-sonnet",
        },
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_duplicate_name_in_team_conflicts(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    cred = await _credential(client, admin, "openai")
    team = await _team(client, admin)
    payload = {
        "name": "dup",
        "provider": "openai",
        "credential_id": cred,
        "type": "chat",
        "provider_model_id": "gpt-4o",
    }
    assert (
        await client.post(f"/teams/{team}/models", json=payload, headers=_bearer(admin))
    ).status_code == HTTP_201_CREATED
    assert (
        await client.post(f"/teams/{team}/models", json=payload, headers=_bearer(admin))
    ).status_code == HTTP_409_CONFLICT


async def test_list_models_pagination(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    cred = await _credential(client, admin, "openai")
    team = await _team(client, admin)
    for i in range(3):
        await client.post(
            f"/teams/{team}/models",
            json={
                "name": f"m{i}",
                "provider": "openai",
                "credential_id": cred,
                "type": "chat",
                "provider_model_id": "gpt-4o",
            },
            headers=_bearer(admin),
        )
    first = await client.get(f"/teams/{team}/models?limit=2", headers=_bearer(admin))
    assert first.status_code == HTTP_200_OK
    assert [m["name"] for m in first.json()] == ["m0", "m1"]
    second = await client.get(f"/teams/{team}/models?limit=2&offset=2", headers=_bearer(admin))
    assert [m["name"] for m in second.json()] == ["m2"]


async def test_list_update_delete(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    cred = await _credential(client, admin, "openai")
    team = await _team(client, admin)
    model_id = (
        await client.post(
            f"/teams/{team}/models",
            json={
                "name": "m1",
                "provider": "openai",
                "credential_id": cred,
                "type": "chat",
                "provider_model_id": "gpt-4o",
            },
            headers=_bearer(admin),
        )
    ).json()["id"]

    listing = await client.get(f"/teams/{team}/models", headers=_bearer(admin))
    assert listing.status_code == HTTP_200_OK
    assert len(listing.json()) == 1

    patched = await client.patch(
        f"/teams/{team}/models/{model_id}",
        json={"enabled": False, "params": {"max_tokens": 256}},
        headers=_bearer(admin),
    )
    assert patched.status_code == HTTP_200_OK
    assert patched.json()["enabled"] is False
    assert patched.json()["params"] == {"max_tokens": 256}

    deleted = await client.delete(f"/teams/{team}/models/{model_id}", headers=_bearer(admin))
    assert deleted.status_code in (200, 204)
    assert (await client.get(f"/teams/{team}/models", headers=_bearer(admin))).json() == []


async def test_non_team_admin_forbidden(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    cred = await _credential(client, admin, "openai")
    team = await _team(client, admin)
    invite = (await client.post("/invites", headers=_bearer(admin))).json()["token"]
    await client.post(
        "/signup",
        json={"invite_token": invite, "email": "out@b.com", "password": "Passw0rd!"},
    )
    outsider = await _login(client, "out@b.com", "Passw0rd!")
    resp = await client.post(
        f"/teams/{team}/models",
        json={
            "name": "x",
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
        },
        headers=_bearer(outsider),
    )
    assert resp.status_code == HTTP_403_FORBIDDEN
