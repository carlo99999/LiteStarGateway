"""Extended RBAC: permission-based team roles + the platform auditor."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_403_FORBIDDEN,
    HTTP_409_CONFLICT,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings, TeamGrant, _env_team_mapping
from litestar_gateway.domain.authorization import (
    AUDITOR_TEAM_PERMISSIONS,
    ROLE_PERMISSIONS,
    Permission,
)
from litestar_gateway.domain.entities import TeamRole

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'rbac.db'}",
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
    invite = (await client.post("/invites", headers=_bearer(admin))).json()["token"]
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


# ── Pure mapping (domain) ────────────────────────────────────────────────────


def test_every_team_role_has_a_permission_set() -> None:
    assert set(ROLE_PERMISSIONS) == set(TeamRole)


def test_admin_holds_every_permission_and_member_none() -> None:
    assert ROLE_PERMISSIONS[TeamRole.ADMIN] == frozenset(Permission)
    assert ROLE_PERMISSIONS[TeamRole.MEMBER] == frozenset()


def test_new_roles_are_scoped_to_their_domain() -> None:
    assert ROLE_PERMISSIONS[TeamRole.MODEL_MANAGER] == frozenset(
        {Permission.MODELS_READ, Permission.MODELS_MANAGE}
    )
    assert ROLE_PERMISSIONS[TeamRole.KEY_ISSUER] == frozenset(
        {Permission.KEYS_READ, Permission.KEYS_ISSUE}
    )
    assert ROLE_PERMISSIONS[TeamRole.BILLING_VIEWER] == frozenset(
        {Permission.USAGE_READ, Permission.BUDGET_READ}
    )


def test_auditor_bypass_is_read_only() -> None:
    assert AUDITOR_TEAM_PERMISSIONS == frozenset({Permission.USAGE_READ, Permission.BUDGET_READ})


def test_sso_team_mapping_accepts_extended_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    team_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setenv(
        "SSO_TEAM_MAPPING",
        json.dumps({"ml-eng": [{"team": team_id, "role": "model-manager"}]}),
    )
    mapping = _env_team_mapping("SSO_TEAM_MAPPING")
    assert mapping["ml-eng"] == (TeamGrant(UUID(team_id), TeamRole.MODEL_MANAGER),)


# ── model-manager ────────────────────────────────────────────────────────────


async def test_model_manager_manages_models_and_nothing_else(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    cred = await _credential(client, admin)
    token = await _member_token(client, admin, team, "mm@corp.com", "model-manager")

    created = await client.post(
        f"/teams/{team}/models", json=_model_payload(cred), headers=_bearer(token)
    )
    assert created.status_code == HTTP_201_CREATED, created.text
    assert (
        await client.get(f"/teams/{team}/models", headers=_bearer(token))
    ).status_code == HTTP_200_OK

    denied = [
        client.post(
            f"/teams/{team}/members",
            json={"email": ADMIN_EMAIL, "role": "member"},
            headers=_bearer(token),
        ),
        client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(token)),
        client.get(f"/teams/{team}/usage", headers=_bearer(token)),
        client.post(
            f"/teams/{team}/service-principals", json={"name": "sp"}, headers=_bearer(token)
        ),
        client.get(f"/teams/{team}/members", headers=_bearer(token)),
    ]
    for request in denied:
        assert (await request).status_code == HTTP_403_FORBIDDEN


# ── key-issuer ───────────────────────────────────────────────────────────────


async def test_key_issuer_mints_lists_and_revokes_keys_only(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    cred = await _credential(client, admin)
    token = await _member_token(client, admin, team, "ki@corp.com", "key-issuer")

    minted = await client.post(f"/teams/{team}/keys", json={"name": "app"}, headers=_bearer(token))
    assert minted.status_code == HTTP_201_CREATED, minted.text
    key_id = minted.json()["id"]
    assert (
        await client.get(f"/teams/{team}/keys", headers=_bearer(token))
    ).status_code == HTTP_200_OK
    revoked = await client.delete(f"/teams/{team}/keys/{key_id}", headers=_bearer(token))
    assert revoked.status_code == HTTP_204_NO_CONTENT

    for request in (
        client.post(f"/teams/{team}/models", json=_model_payload(cred), headers=_bearer(token)),
        client.get(f"/teams/{team}/usage", headers=_bearer(token)),
        client.get(f"/teams/{team}/members", headers=_bearer(token)),
    ):
        assert (await request).status_code == HTTP_403_FORBIDDEN


# ── billing-viewer ───────────────────────────────────────────────────────────


async def test_billing_viewer_reads_usage_budget_and_spend_only(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    cred = await _credential(client, admin)
    set_budget = await client.put(
        f"/teams/{team}/budget",
        json={"limit_cost": 100.0, "window": "monthly"},
        headers=_bearer(admin),
    )
    assert set_budget.status_code in (HTTP_200_OK, HTTP_201_CREATED), set_budget.text
    token = await _member_token(client, admin, team, "bv@corp.com", "billing-viewer")

    for request in (
        client.get(f"/teams/{team}/usage", headers=_bearer(token)),
        client.get(f"/teams/{team}/budget", headers=_bearer(token)),
        client.get(f"/teams/{team}/keys/spending", headers=_bearer(token)),
    ):
        assert (await request).status_code == HTTP_200_OK

    for request in (
        client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(token)),
        client.post(f"/teams/{team}/models", json=_model_payload(cred), headers=_bearer(token)),
        client.put(
            f"/teams/{team}/budget",
            json={"limit_cost": 1.0, "window": "monthly"},
            headers=_bearer(token),
        ),
        client.get(f"/teams/{team}/members", headers=_bearer(token)),
    ):
        assert (await request).status_code == HTTP_403_FORBIDDEN


# ── member & admin unchanged ─────────────────────────────────────────────────


async def test_plain_member_is_still_denied_all_management(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    token = await _member_token(client, admin, team, "plain@corp.com", "member")

    for request in (
        client.get(f"/teams/{team}/members", headers=_bearer(token)),
        client.get(f"/teams/{team}/models", headers=_bearer(token)),
        client.get(f"/teams/{team}/keys", headers=_bearer(token)),
        client.get(f"/teams/{team}/usage", headers=_bearer(token)),
        client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(token)),
    ):
        assert (await request).status_code == HTTP_403_FORBIDDEN


async def test_team_admin_keeps_full_access(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    cred = await _credential(client, admin)
    token = await _member_token(client, admin, team, "lead@corp.com", "admin")

    assert (
        await client.post(
            f"/teams/{team}/models", json=_model_payload(cred), headers=_bearer(token)
        )
    ).status_code == HTTP_201_CREATED
    assert (
        await client.get(f"/teams/{team}/members", headers=_bearer(token))
    ).status_code == HTTP_200_OK
    assert (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(token))
    ).status_code == HTTP_201_CREATED


async def test_last_admin_cannot_be_demoted_to_extended_role(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    admin_id = (await client.get("/me", headers=_bearer(admin))).json()["id"]

    resp = await client.patch(
        f"/teams/{team}/members/{admin_id}",
        json={"role": "billing-viewer"},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_409_CONFLICT


# ── Platform auditor ─────────────────────────────────────────────────────────


async def test_admin_grants_and_revokes_auditor_with_audit_trail(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    user_id = await _signup(client, admin, "aud@corp.com")

    granted = await client.patch(
        f"/users/{user_id}/auditor", json={"is_auditor": True}, headers=_bearer(admin)
    )
    assert granted.status_code == HTTP_200_OK, granted.text
    assert granted.json()["is_auditor"] is True

    events = (await client.get("/audit", headers=_bearer(admin))).json()
    assert "user.grant_auditor" in {e["action"] for e in events}


async def test_auditor_reads_audit_and_any_team_usage_but_mutates_nothing(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    cred = await _credential(client, admin)
    user_id = await _signup(client, admin, "aud@corp.com")
    await client.patch(
        f"/users/{user_id}/auditor", json={"is_auditor": True}, headers=_bearer(admin)
    )
    token = await _login(client, "aud@corp.com", "Sup3r-Secret!")

    # Read-only, cross-team (the auditor is NOT a member of the team).
    assert (await client.get("/audit", headers=_bearer(token))).status_code == HTTP_200_OK
    assert (
        await client.get(f"/teams/{team}/usage", headers=_bearer(token))
    ).status_code == HTTP_200_OK
    assert (
        await client.get(f"/teams/{team}/keys/spending", headers=_bearer(token))
    ).status_code == HTTP_200_OK

    for request in (
        client.post(f"/teams/{team}/models", json=_model_payload(cred), headers=_bearer(token)),
        client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(token)),
        client.post("/organizations", json={"name": "Nope"}, headers=_bearer(token)),
    ):
        assert (await request).status_code == HTTP_403_FORBIDDEN


async def test_non_admin_cannot_grant_auditor_and_plain_user_cannot_read_audit(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    user_id = await _signup(client, admin, "plain@corp.com")
    token = await _login(client, "plain@corp.com", "Sup3r-Secret!")

    assert (await client.get("/audit", headers=_bearer(token))).status_code == HTTP_403_FORBIDDEN
    denied = await client.patch(
        f"/users/{user_id}/auditor", json={"is_auditor": True}, headers=_bearer(token)
    )
    assert denied.status_code == HTTP_403_FORBIDDEN


async def test_revoking_auditor_closes_audit_access(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    user_id = await _signup(client, admin, "aud@corp.com")
    await client.patch(
        f"/users/{user_id}/auditor", json={"is_auditor": True}, headers=_bearer(admin)
    )
    token = await _login(client, "aud@corp.com", "Sup3r-Secret!")
    assert (await client.get("/audit", headers=_bearer(token))).status_code == HTTP_200_OK

    revoked = await client.patch(
        f"/users/{user_id}/auditor", json={"is_auditor": False}, headers=_bearer(admin)
    )
    assert revoked.status_code == HTTP_200_OK
    assert revoked.json()["is_auditor"] is False
    assert (await client.get("/audit", headers=_bearer(token))).status_code == HTTP_403_FORBIDDEN
