"""Integration tests for organizations, teams and memberships."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from _invite_helpers import seed_team_and_invite
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
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
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'teams.db'}",
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
    return (await client.post("/login", json={"email": email, "password": password})).json()[
        "access_token"
    ]


async def _register(client: AsyncTestClient, admin_token: str, email: str) -> None:
    invite = await seed_team_and_invite(client, admin_token)
    await client.post(
        "/signup",
        json={"invite_token": invite, "email": email, "password": "Passw0rd!"},
    )


async def _org(client: AsyncTestClient, token: str) -> str:
    return (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(token))
    ).json()["id"]


async def _team(client: AsyncTestClient, token: str, org_id: str, admin_email: str) -> str:
    return (
        await client.post(
            f"/organizations/{org_id}/teams",
            json={"name": "Core", "admin_email": admin_email},
            headers=_bearer(token),
        )
    ).json()["id"]


async def test_only_platform_admin_creates_org(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    await _register(client, admin, "bob@b.com")
    bob = await _login(client, "bob@b.com", "Passw0rd!")
    assert (
        await client.post("/organizations", json={"name": "X"}, headers=_bearer(bob))
    ).status_code == HTTP_403_FORBIDDEN
    assert (
        await client.post("/organizations", json={"name": "X"}, headers=_bearer(admin))
    ).status_code == HTTP_201_CREATED


async def test_create_team_assigns_first_admin(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    await _register(client, admin, "lead@b.com")
    org_id = await _org(client, admin)
    resp = await client.post(
        f"/organizations/{org_id}/teams",
        json={"name": "Core", "admin_email": "lead@b.com"},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_201_CREATED
    team_id = resp.json()["id"]
    # 'lead' is team admin → can list members.
    lead = await _login(client, "lead@b.com", "Passw0rd!")
    members = await client.get(f"/teams/{team_id}/members", headers=_bearer(lead))
    assert members.status_code == HTTP_200_OK
    # Both the platform admin (first admin) and the named lead are team admins.
    assert len(members.json()) == 2
    assert all(m["role"] == "admin" for m in members.json())


async def test_create_team_unknown_admin_email_404(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = await _org(client, admin)
    resp = await client.post(
        f"/organizations/{org_id}/teams",
        json={"name": "Core", "admin_email": "ghost@b.com"},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_404_NOT_FOUND


async def test_team_admin_manages_members(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    await _register(client, admin, "lead@b.com")
    await _register(client, admin, "member@b.com")
    org_id = await _org(client, admin)
    team_id = await _team(client, admin, org_id, "lead@b.com")
    lead = await _login(client, "lead@b.com", "Passw0rd!")

    add = await client.post(
        f"/teams/{team_id}/members",
        json={"email": "member@b.com", "role": "member"},
        headers=_bearer(lead),
    )
    assert add.status_code == HTTP_201_CREATED
    user_id = add.json()["user_id"]

    promote = await client.patch(
        f"/teams/{team_id}/members/{user_id}",
        json={"role": "admin"},
        headers=_bearer(lead),
    )
    assert promote.status_code == HTTP_200_OK
    assert promote.json()["role"] == "admin"

    remove = await client.delete(f"/teams/{team_id}/members/{user_id}", headers=_bearer(lead))
    assert remove.status_code in (200, 204)
    members = await client.get(f"/teams/{team_id}/members", headers=_bearer(lead))
    assert len(members.json()) == 2  # the platform admin and the lead remain


async def test_plain_member_cannot_manage(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    await _register(client, admin, "lead@b.com")
    await _register(client, admin, "member@b.com")
    await _register(client, admin, "stranger@b.com")
    org_id = await _org(client, admin)
    team_id = await _team(client, admin, org_id, "lead@b.com")
    lead = await _login(client, "lead@b.com", "Passw0rd!")
    await client.post(
        f"/teams/{team_id}/members",
        json={"email": "member@b.com", "role": "member"},
        headers=_bearer(lead),
    )
    member = await _login(client, "member@b.com", "Passw0rd!")
    # A plain member cannot add others.
    resp = await client.post(
        f"/teams/{team_id}/members",
        json={"email": "stranger@b.com", "role": "member"},
        headers=_bearer(member),
    )
    assert resp.status_code == HTTP_403_FORBIDDEN


async def test_platform_admin_manages_any_team(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    await _register(client, admin, "lead@b.com")
    await _register(client, admin, "newbie@b.com")
    org_id = await _org(client, admin)
    team_id = await _team(client, admin, org_id, "lead@b.com")
    # The platform admin is a team admin by default and can add members.
    resp = await client.post(
        f"/teams/{team_id}/members",
        json={"email": "newbie@b.com", "role": "member"},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_201_CREATED


async def test_list_members_pagination(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = await _org(client, admin)
    team_id = await _team(client, admin, org_id, ADMIN_EMAIL)
    for i in range(3):
        email = f"m{i}@corp.com"
        await _register(client, admin, email)
        await client.post(
            f"/teams/{team_id}/members",
            json={"email": email, "role": "member"},
            headers=_bearer(admin),
        )
    first = await client.get(f"/teams/{team_id}/members?limit=2", headers=_bearer(admin))
    assert first.status_code == HTTP_200_OK
    assert len(first.json()) == 2
    # 1 team admin (bootstrap) + 3 members = 4 total; page 2 holds the rest.
    second = await client.get(f"/teams/{team_id}/members?limit=2&offset=2", headers=_bearer(admin))
    assert len(second.json()) == 2


async def test_list_organizations_pagination(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    for name in ("A", "B", "C"):
        await client.post("/organizations", json={"name": name}, headers=_bearer(admin))
    first = await client.get("/organizations?limit=2", headers=_bearer(admin))
    assert first.status_code == HTTP_200_OK
    assert [o["name"] for o in first.json()] == ["A", "B"]
    second = await client.get("/organizations?limit=2&offset=2", headers=_bearer(admin))
    assert [o["name"] for o in second.json()] == ["C"]


async def test_list_all_teams_platform_admin(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = await _org(client, admin)
    core = await _team(client, admin, org_id, ADMIN_EMAIL)
    ops = (
        await client.post(
            f"/organizations/{org_id}/teams",
            json={"name": "Ops", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]

    resp = await client.get("/teams", headers=_bearer(admin))
    assert resp.status_code == HTTP_200_OK, resp.text
    ids = {t["id"] for t in resp.json()}
    assert {core, ops} <= ids


async def test_list_all_teams_forbidden_for_non_admin(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    await _register(client, admin, "bob@b.com")
    bob = await _login(client, "bob@b.com", "Passw0rd!")
    assert (await client.get("/teams", headers=_bearer(bob))).status_code == HTTP_403_FORBIDDEN


async def test_get_team_detail(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = await _org(client, admin)
    team_id = await _team(client, admin, org_id, ADMIN_EMAIL)

    resp = await client.get(f"/teams/{team_id}", headers=_bearer(admin))
    assert resp.status_code == HTTP_200_OK, resp.text
    body = resp.json()
    assert body["id"] == team_id
    assert body["organization_id"] == org_id
    assert body["name"] == "Core"


async def test_get_team_missing_is_404(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    resp = await client.get(f"/teams/{uuid4()}", headers=_bearer(admin))
    assert resp.status_code == HTTP_404_NOT_FOUND, resp.text


async def test_rename_team(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = await _org(client, admin)
    team_id = await _team(client, admin, org_id, ADMIN_EMAIL)

    resp = await client.patch(f"/teams/{team_id}", json={"name": "Renamed"}, headers=_bearer(admin))
    assert resp.status_code == HTTP_200_OK, resp.text
    assert resp.json()["name"] == "Renamed"
    got = await client.get(f"/teams/{team_id}", headers=_bearer(admin))
    assert got.json()["name"] == "Renamed"


async def test_rename_team_missing_is_404(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    resp = await client.patch(f"/teams/{uuid4()}", json={"name": "X"}, headers=_bearer(admin))
    assert resp.status_code == HTTP_404_NOT_FOUND, resp.text


async def test_delete_empty_team(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = await _org(client, admin)
    team_id = await _team(client, admin, org_id, ADMIN_EMAIL)

    resp = await client.delete(f"/teams/{team_id}", headers=_bearer(admin))
    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text
    # Gone, along with its membership.
    assert (
        await client.get(f"/teams/{team_id}", headers=_bearer(admin))
    ).status_code == HTTP_404_NOT_FOUND


async def test_delete_team_blocked_by_api_key(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = await _org(client, admin)
    team_id = await _team(client, admin, org_id, ADMIN_EMAIL)
    key = await client.post(
        f"/teams/{team_id}/keys",
        json={"name": "k", "scope": "inference"},
        headers=_bearer(admin),
    )
    assert key.status_code in (HTTP_200_OK, HTTP_201_CREATED), key.text

    resp = await client.delete(f"/teams/{team_id}", headers=_bearer(admin))
    assert resp.status_code == HTTP_409_CONFLICT, resp.text
    # Still there.
    assert (
        await client.get(f"/teams/{team_id}", headers=_bearer(admin))
    ).status_code == HTTP_200_OK


async def test_team_rename_and_delete_forbidden_for_non_admin(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = await _org(client, admin)
    team_id = await _team(client, admin, org_id, ADMIN_EMAIL)
    await _register(client, admin, "bob@b.com")
    bob = await _login(client, "bob@b.com", "Passw0rd!")

    assert (
        await client.patch(f"/teams/{team_id}", json={"name": "X"}, headers=_bearer(bob))
    ).status_code == HTTP_403_FORBIDDEN
    assert (
        await client.delete(f"/teams/{team_id}", headers=_bearer(bob))
    ).status_code == HTTP_403_FORBIDDEN


async def test_create_team_with_metadata(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = await _org(client, admin)
    resp = await client.post(
        f"/organizations/{org_id}/teams",
        json={
            "name": "Meta",
            "admin_email": ADMIN_EMAIL,
            "description": "the meta team",
            "tags": ["core", "eu"],
        },
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
    body = resp.json()
    assert body["description"] == "the meta team"
    assert body["tags"] == ["core", "eu"]


async def test_create_team_defaults_empty_metadata(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = await _org(client, admin)
    team_id = await _team(client, admin, org_id, ADMIN_EMAIL)
    got = (await client.get(f"/teams/{team_id}", headers=_bearer(admin))).json()
    assert got["description"] is None
    assert got["tags"] == []


async def test_update_team_metadata(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = await _org(client, admin)
    team_id = await _team(client, admin, org_id, ADMIN_EMAIL)
    resp = await client.patch(
        f"/teams/{team_id}",
        json={"name": "Core", "description": "d2", "tags": ["x"]},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    assert resp.json()["description"] == "d2"
    assert resp.json()["tags"] == ["x"]
    got = await client.get(f"/teams/{team_id}", headers=_bearer(admin))
    assert got.json()["tags"] == ["x"]


async def test_create_and_update_rate_limit_rpm(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = await _org(client, admin)
    created = await client.post(
        f"/organizations/{org_id}/teams",
        json={"name": "Capped", "admin_email": ADMIN_EMAIL, "rate_limit_rpm": 100},
        headers=_bearer(admin),
    )
    assert created.status_code == HTTP_201_CREATED, created.text
    team_id = created.json()["id"]
    assert created.json()["rate_limit_rpm"] == 100

    # Update clears it (null = unlimited).
    updated = await client.patch(
        f"/teams/{team_id}",
        json={"name": "Capped", "rate_limit_rpm": None},
        headers=_bearer(admin),
    )
    assert updated.status_code == HTTP_200_OK, updated.text
    assert updated.json()["rate_limit_rpm"] is None
