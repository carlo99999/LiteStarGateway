"""Personal keys vs service-principal keys.

- A **personal key** (`POST /teams/{id}/keys`) belongs to its creating user and
  is inference-only; it is auto-revoked when that user is deactivated.
- A **service principal** is a team-owned, named machine identity. Only keys
  issued under an *enabled* SP may hold management/all scope; disabling the SP
  stops its keys. SP administration (create/enable/disable/issue key) is
  JWT-only — a management key cannot create SPs or mint more keys.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
DEV_PASSWORD = "S3cure-pass-123"  # pragma: allowlist secret


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sp.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


async def _admin(client: AsyncTestClient) -> str:
    return (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]


async def _team(client: AsyncTestClient, admin: str, name: str = "Core") -> tuple[str, str]:
    cred = (
        await client.post(
            "/credentials",
            json={
                "name": f"c-{name}",
                "provider": "openai",
                "values": {"api_key": "sk-x"},  # pragma: allowlist secret
            },
            headers=_bearer(admin),
        )
    ).json()["id"]
    org = (
        await client.post("/organizations", json={"name": f"O-{name}"}, headers=_bearer(admin))
    ).json()["id"]
    team = (
        await client.post(
            f"/organizations/{org}/teams",
            json={"name": name, "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    return team, cred


async def _sp(client: AsyncTestClient, admin: str, team: str, name: str = "ci") -> str:
    resp = await client.post(
        f"/teams/{team}/service-principals", json={"name": name}, headers=_bearer(admin)
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
    return resp.json()["id"]


async def _sp_key(
    client: AsyncTestClient, admin: str, team: str, sp: str, scope: str = "management"
) -> str:
    resp = await client.post(
        f"/teams/{team}/service-principals/{sp}/keys",
        json={"name": "k", "scope": scope},
        headers=_bearer(admin),
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["plaintext"]


def _model(cred: str, name: str = "m") -> dict:
    return {
        "name": name,
        "provider": "openai",
        "credential_id": cred,
        "type": "chat",
        "provider_model_id": "gpt-4o",
        "enabled": True,
    }


# ---- personal keys ---------------------------------------------------------


async def test_personal_key_is_inference_only(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _team(client, admin)
    resp = await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    assert resp.status_code == HTTP_201_CREATED
    assert resp.json()["scope"] == "inference"


async def test_personal_key_cannot_request_management(client: AsyncTestClient) -> None:
    # Management belongs to a service principal, not a personal key.
    admin = await _admin(client)
    team, _ = await _team(client, admin)
    resp = await client.post(
        f"/teams/{team}/keys", json={"name": "k", "scope": "management"}, headers=_bearer(admin)
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_personal_keys_revoked_when_user_deactivated(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _team(client, admin)
    # Onboard a member and give them a personal (inference) key.
    invite = (await client.post("/invites", headers=_bearer(admin))).json()["token"]
    await client.post(
        "/signup",
        json={"invite_token": invite, "email": "dev@example.com", "password": DEV_PASSWORD},
    )
    await client.post(
        f"/teams/{team}/members",
        json={"email": "dev@example.com", "role": "admin"},
        headers=_bearer(admin),
    )
    dev = (
        await client.post("/login", json={"email": "dev@example.com", "password": DEV_PASSWORD})
    ).json()["access_token"]
    await client.post(f"/teams/{team}/models", json=_model(cred), headers=_bearer(admin))
    # A personal key created by the dev — owned/attributed to them (created_by).
    dev_key = (
        await client.post(f"/teams/{team}/keys", json={"name": "devk"}, headers=_bearer(dev))
    ).json()["plaintext"]

    # Works before deactivation.
    ok = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(dev_key),
    )
    assert ok.status_code not in (HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN)

    # Find the dev's user id and disable them.
    members = (await client.get(f"/teams/{team}/members", headers=_bearer(admin))).json()
    admin_id = (await client.get("/me", headers=_bearer(admin))).json()["id"]
    dev_id = next(m["user_id"] for m in members if m["user_id"] != admin_id)
    await client.patch(f"/users/{dev_id}", json={"is_active": False}, headers=_bearer(admin))

    # The personal key is now revoked.
    denied = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(dev_key),
    )
    assert denied.status_code == HTTP_401_UNAUTHORIZED


# ---- service principals ----------------------------------------------------


async def test_sp_key_can_manage_its_team(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _team(client, admin)
    sp = await _sp(client, admin, team)
    key = await _sp_key(client, admin, team, sp, "management")

    created = await client.post(f"/teams/{team}/models", json=_model(cred), headers=_bearer(key))
    assert created.status_code == HTTP_201_CREATED, created.text


async def test_disabled_sp_key_cannot_manage(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _team(client, admin)
    sp = await _sp(client, admin, team)
    key = await _sp_key(client, admin, team, sp, "management")

    await client.patch(
        f"/teams/{team}/service-principals/{sp}",
        json={"enabled": False},
        headers=_bearer(admin),
    )
    denied = await client.post(f"/teams/{team}/models", json=_model(cred), headers=_bearer(key))
    assert denied.status_code == HTTP_403_FORBIDDEN


async def test_sp_is_scoped_to_its_own_team(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team_a, cred = await _team(client, admin, "A")
    team_b, _ = await _team(client, admin, "B")
    sp_a = await _sp(client, admin, team_a)
    key_a = await _sp_key(client, admin, team_a, sp_a, "management")

    resp = await client.post(f"/teams/{team_b}/models", json=_model(cred), headers=_bearer(key_a))
    assert resp.status_code == HTTP_403_FORBIDDEN


async def test_sp_key_can_be_inference_scope(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _team(client, admin)
    sp = await _sp(client, admin, team)
    key = await _sp_key(client, admin, team, sp, "inference")
    await client.post(f"/teams/{team}/models", json=_model(cred), headers=_bearer(admin))

    # Inference works; management is denied (scope, not SP).
    infer = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(key),
    )
    assert infer.status_code not in (HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN)
    manage = await client.post(
        f"/teams/{team}/models", json=_model(cred, "m2"), headers=_bearer(key)
    )
    assert manage.status_code == HTTP_403_FORBIDDEN


async def test_management_sp_key_cannot_administer_service_principals(
    client: AsyncTestClient,
) -> None:
    # No self-replication: SP administration (create SP, issue SP keys) is
    # JWT-only, even for a management-scoped key.
    admin = await _admin(client)
    team, _ = await _team(client, admin)
    sp = await _sp(client, admin, team)
    key = await _sp_key(client, admin, team, sp, "management")

    create_sp = await client.post(
        f"/teams/{team}/service-principals", json={"name": "evil"}, headers=_bearer(key)
    )
    assert create_sp.status_code == HTTP_401_UNAUTHORIZED
    mint = await client.post(
        f"/teams/{team}/service-principals/{sp}/keys",
        json={"name": "evil"},
        headers=_bearer(key),
    )
    assert mint.status_code == HTTP_401_UNAUTHORIZED
    # Nor a personal key on the same team.
    personal = await client.post(f"/teams/{team}/keys", json={"name": "p"}, headers=_bearer(key))
    assert personal.status_code == HTTP_401_UNAUTHORIZED


async def test_sp_audit_attributes_the_principal_identity(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _team(client, admin)
    sp = await _sp(client, admin, team, "deployer")
    key = await _sp_key(client, admin, team, sp, "management")
    await client.post(f"/teams/{team}/models", json=_model(cred), headers=_bearer(key))

    audit = (await client.get("/audit", headers=_bearer(admin))).json()
    entry = next(e for e in audit if e["action"] == "model.create")
    assert entry["actor_type"] == "service_principal"
    assert entry["actor_email"] == "sp:deployer"


async def test_sp_crud_and_listing(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, _ = await _team(client, admin)
    sp = await _sp(client, admin, team, "one")

    listed = await client.get(f"/teams/{team}/service-principals", headers=_bearer(admin))
    assert listed.status_code == HTTP_200_OK
    assert [s["name"] for s in listed.json()] == ["one"]
    assert listed.json()[0]["enabled"] is True

    patched = await client.patch(
        f"/teams/{team}/service-principals/{sp}",
        json={"enabled": False},
        headers=_bearer(admin),
    )
    assert patched.status_code == HTTP_200_OK
    assert patched.json()["enabled"] is False


async def test_sp_creation_requires_team_management(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, _ = await _team(client, admin)
    # A plain member (not team admin) cannot create SPs.
    invite = (await client.post("/invites", headers=_bearer(admin))).json()["token"]
    await client.post(
        "/signup",
        json={"invite_token": invite, "email": "m@example.com", "password": DEV_PASSWORD},
    )
    await client.post(
        f"/teams/{team}/members",
        json={"email": "m@example.com", "role": "member"},
        headers=_bearer(admin),
    )
    member = (
        await client.post("/login", json={"email": "m@example.com", "password": DEV_PASSWORD})
    ).json()["access_token"]
    resp = await client.post(
        f"/teams/{team}/service-principals", json={"name": "x"}, headers=_bearer(member)
    )
    assert resp.status_code == HTTP_403_FORBIDDEN
