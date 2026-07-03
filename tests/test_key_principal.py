"""API keys as team service principals (Databricks-style), with scopes.

A key belongs to a team, full stop (`created_by` is provenance metadata, not
ownership). Its `scope` bounds what it may do:
- `inference` (default): only the `/v1/*` endpoints — today's behavior.
- `management`: acts as a team admin *of its own team only* on team-scoped
  management endpoints (models CRUD, usage/spending/budget reads). Never a
  platform admin: credentials, orgs, audit, invites and key minting stay
  JWT-only (a leaked key must not be able to self-replicate or escalate).
- `all`: both.
"""

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


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin(client: AsyncTestClient) -> str:
    return (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]


async def _setup_team(client: AsyncTestClient, admin: str, name: str = "Core") -> tuple[str, str]:
    """Org + team; returns (team_id, credential_id)."""
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


async def _issue_key(client: AsyncTestClient, admin: str, team: str, scope: str | None) -> str:
    """Inference (or default) → a personal key; management/all → a key under a
    service principal (the only kind allowed to hold management scope)."""
    if scope in (None, "inference"):
        body: dict = {"name": "k"}
        if scope is not None:
            body["scope"] = scope
        resp = await client.post(f"/teams/{team}/keys", json=body, headers=_bearer(admin))
        assert resp.status_code in (200, 201), resp.text
        return resp.json()["plaintext"]
    sp = (
        await client.post(
            f"/teams/{team}/service-principals",
            json={"name": f"sp-{scope}"},
            headers=_bearer(admin),
        )
    ).json()["id"]
    resp = await client.post(
        f"/teams/{team}/service-principals/{sp}/keys",
        json={"name": f"k-{scope}", "scope": scope},
        headers=_bearer(admin),
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["plaintext"]


def _model_body(cred: str, name: str = "m") -> dict:
    return {
        "name": name,
        "provider": "openai",
        "credential_id": cred,
        "type": "chat",
        "provider_model_id": "gpt-4o",
        "enabled": True,
    }


async def test_key_scope_defaults_to_inference(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, _ = await _setup_team(client, admin)
    resp = await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    assert resp.json()["scope"] == "inference"


async def test_invalid_scope_is_rejected(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, _ = await _setup_team(client, admin)
    resp = await client.post(
        f"/teams/{team}/keys", json={"name": "k", "scope": "root"}, headers=_bearer(admin)
    )
    assert resp.status_code == 400


async def test_management_key_can_manage_models_on_its_team(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _setup_team(client, admin)
    key = await _issue_key(client, admin, team, "management")

    created = await client.post(
        f"/teams/{team}/models", json=_model_body(cred), headers=_bearer(key)
    )
    assert created.status_code == HTTP_201_CREATED, created.text

    listed = await client.get(f"/teams/{team}/models", headers=_bearer(key))
    assert listed.status_code == HTTP_200_OK
    assert [m["name"] for m in listed.json()] == ["m"]


async def test_management_key_is_scoped_to_its_own_team(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team_a, cred = await _setup_team(client, admin, "A")
    team_b, _ = await _setup_team(client, admin, "B")
    key_a = await _issue_key(client, admin, team_a, "management")

    resp = await client.post(
        f"/teams/{team_b}/models", json=_model_body(cred), headers=_bearer(key_a)
    )
    assert resp.status_code == HTTP_403_FORBIDDEN


async def test_management_key_can_read_usage_and_budget(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, _ = await _setup_team(client, admin)
    key = await _issue_key(client, admin, team, "management")
    await client.put(
        f"/teams/{team}/budget",
        json={"limit_cost": 5.0, "window": "monthly"},
        headers=_bearer(admin),
    )

    assert (await client.get(f"/teams/{team}/usage", headers=_bearer(key))).status_code == 200
    budget = await client.get(f"/teams/{team}/budget", headers=_bearer(key))
    assert budget.status_code == HTTP_200_OK
    assert budget.json()["limit_cost"] == 5.0


async def test_management_key_cannot_escalate(client: AsyncTestClient) -> None:
    # No self-replication (minting keys), no platform surface (credentials),
    # no budget writes (platform-admin only) — even with management scope.
    admin = await _admin(client)
    team, _ = await _setup_team(client, admin)
    key = await _issue_key(client, admin, team, "management")

    mint = await client.post(f"/teams/{team}/keys", json={"name": "evil"}, headers=_bearer(key))
    assert mint.status_code == HTTP_401_UNAUTHORIZED

    creds = await client.get("/credentials", headers=_bearer(key))
    assert creds.status_code == HTTP_401_UNAUTHORIZED

    budget = await client.put(
        f"/teams/{team}/budget",
        json={"limit_cost": 999.0, "window": "monthly"},
        headers=_bearer(key),
    )
    assert budget.status_code == HTTP_401_UNAUTHORIZED


async def test_management_key_cannot_do_inference(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _setup_team(client, admin)
    await client.post(f"/teams/{team}/models", json=_model_body(cred), headers=_bearer(admin))
    key = await _issue_key(client, admin, team, "management")

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(key),
    )
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_inference_key_cannot_manage(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _setup_team(client, admin)
    key = await _issue_key(client, admin, team, None)  # default: inference

    resp = await client.post(f"/teams/{team}/models", json=_model_body(cred), headers=_bearer(key))
    assert resp.status_code == HTTP_403_FORBIDDEN


async def test_all_scope_can_do_both(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _setup_team(client, admin)
    key = await _issue_key(client, admin, team, "all")

    created = await client.post(
        f"/teams/{team}/models", json=_model_body(cred), headers=_bearer(key)
    )
    assert created.status_code == HTTP_201_CREATED
    # Inference reaches model resolution (the fake credential fails later at the
    # provider SDK, but auth/scope must not be the blocker) — anything but
    # 401/403 proves the key passed the gate.
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(key),
    )
    assert resp.status_code not in (HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN)


async def test_jwt_still_works_on_management_endpoints(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _setup_team(client, admin)
    resp = await client.post(
        f"/teams/{team}/models", json=_model_body(cred), headers=_bearer(admin)
    )
    assert resp.status_code == HTTP_201_CREATED


async def test_audit_attributes_the_key_principal(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _setup_team(client, admin)
    key = await _issue_key(client, admin, team, "management")
    await client.post(f"/teams/{team}/models", json=_model_body(cred), headers=_bearer(key))

    audit = await client.get("/audit", headers=_bearer(admin))
    assert audit.status_code == HTTP_200_OK
    entries = [e for e in audit.json() if e["action"] == "model.create"]
    assert entries, audit.text
    # A management key belongs to a service principal → attributed to that
    # identity, with actor_type as the discriminator (never joined vs users).
    assert entries[0]["actor_email"].startswith("sp:")
    assert entries[0]["actor_type"] == "service_principal"
    human = [e for e in audit.json() if e["action"] == "team.create"]
    assert human and human[0]["actor_type"] == "user"


async def test_all_scope_key_is_still_confined_to_its_team(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team_a, cred = await _setup_team(client, admin, "A")
    team_b, _ = await _setup_team(client, admin, "B")
    key_a = await _issue_key(client, admin, team_a, "all")

    resp = await client.post(
        f"/teams/{team_b}/models", json=_model_body(cred), headers=_bearer(key_a)
    )
    assert resp.status_code == HTTP_403_FORBIDDEN


async def test_scope_is_case_sensitive(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, _ = await _setup_team(client, admin)
    for bad in ("Management", " management", "ALL"):
        resp = await client.post(
            f"/teams/{team}/keys", json={"name": "k", "scope": bad}, headers=_bearer(admin)
        )
        assert resp.status_code == 400, bad


async def test_revoked_management_key_is_rejected_on_management_endpoints(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    team, cred = await _setup_team(client, admin)
    sp = (
        await client.post(
            f"/teams/{team}/service-principals", json={"name": "sp"}, headers=_bearer(admin)
        )
    ).json()["id"]
    resp = await client.post(
        f"/teams/{team}/service-principals/{sp}/keys",
        json={"name": "k", "scope": "management"},
        headers=_bearer(admin),
    )
    key_id, plaintext = resp.json()["id"], resp.json()["plaintext"]
    await client.delete(f"/teams/{team}/keys/{key_id}", headers=_bearer(admin))

    denied = await client.post(
        f"/teams/{team}/models", json=_model_body(cred), headers=_bearer(plaintext)
    )
    assert denied.status_code == HTTP_401_UNAUTHORIZED
