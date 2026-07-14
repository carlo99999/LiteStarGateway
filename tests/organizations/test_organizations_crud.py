"""Integration tests for organization CRUD (platform-admin only).

Covers create/list already exercised elsewhere plus the new rename (PATCH) and
delete (DELETE): the not-empty guard (409, no cascade), not-found (404), and the
platform-admin gate (403 for a non-admin).
"""

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
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"  # pragma: allowlist secret
MEMBER_PASSWORD = "Passw0rd!"  # pragma: allowlist secret


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'orgs.db'}",
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


async def _admin(client: AsyncTestClient) -> str:
    return await _login(client, ADMIN_EMAIL, MASTER_KEY)


async def _member(client: AsyncTestClient, admin: str, email: str) -> str:
    """Register a plain (non-admin) user via an invite and log them in."""
    invite = await seed_team_and_invite(client, admin)
    await client.post(
        "/signup",
        json={"invite_token": invite, "email": email, "password": MEMBER_PASSWORD},
    )
    return await _login(client, email, MEMBER_PASSWORD)


async def _create_org(client: AsyncTestClient, admin: str, name: str) -> str:
    resp = await client.post("/organizations", json={"name": name}, headers=_bearer(admin))
    assert resp.status_code == HTTP_201_CREATED, resp.text
    return resp.json()["id"]


async def test_rename_updates_the_name(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    org_id = await _create_org(client, admin, "Acme")

    resp = await client.patch(
        f"/organizations/{org_id}", json={"name": "Acme Corp"}, headers=_bearer(admin)
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    assert resp.json()["name"] == "Acme Corp"

    listed = (await client.get("/organizations", headers=_bearer(admin))).json()
    assert {o["id"]: o["name"] for o in listed}[org_id] == "Acme Corp"


async def test_create_with_description_and_tags(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    resp = await client.post(
        "/organizations",
        json={"name": "Meta", "description": "an org", "tags": ["eu", "prod"]},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
    body = resp.json()
    assert body["description"] == "an org"
    assert body["tags"] == ["eu", "prod"]


async def test_create_defaults_empty_metadata(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    org_id = await _create_org(client, admin, "Bare")
    got = (await client.get(f"/organizations/{org_id}", headers=_bearer(admin))).json()
    assert got["description"] is None
    assert got["tags"] == []


async def test_update_replaces_description_and_tags(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    org_id = await _create_org(client, admin, "Meta2")
    resp = await client.patch(
        f"/organizations/{org_id}",
        json={"name": "Meta2", "description": "d2", "tags": ["x"]},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    assert resp.json()["description"] == "d2"
    assert resp.json()["tags"] == ["x"]
    # Persisted, and re-fetch confirms it.
    got = (await client.get(f"/organizations/{org_id}", headers=_bearer(admin))).json()
    assert got["tags"] == ["x"]


async def test_delete_empty_org(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    org_id = await _create_org(client, admin, "Temp")

    resp = await client.delete(f"/organizations/{org_id}", headers=_bearer(admin))
    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text

    listed = (await client.get("/organizations", headers=_bearer(admin))).json()
    assert org_id not in {o["id"] for o in listed}


async def test_delete_org_with_teams_is_conflict(client: AsyncTestClient) -> None:
    # Non-empty org must not be deletable — no cascade; the teams would be orphaned.
    admin = await _admin(client)
    org_id = await _create_org(client, admin, "HasTeams")
    team = await client.post(
        f"/organizations/{org_id}/teams",
        json={"name": "Core", "admin_email": ADMIN_EMAIL},
        headers=_bearer(admin),
    )
    assert team.status_code == HTTP_201_CREATED, team.text

    resp = await client.delete(f"/organizations/{org_id}", headers=_bearer(admin))
    assert resp.status_code == HTTP_409_CONFLICT, resp.text

    # Still there.
    listed = (await client.get("/organizations", headers=_bearer(admin))).json()
    assert org_id in {o["id"] for o in listed}


async def test_rename_missing_org_is_404(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    resp = await client.patch(
        f"/organizations/{uuid4()}", json={"name": "X"}, headers=_bearer(admin)
    )
    assert resp.status_code == HTTP_404_NOT_FOUND, resp.text


async def test_delete_missing_org_is_404(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    resp = await client.delete(f"/organizations/{uuid4()}", headers=_bearer(admin))
    assert resp.status_code == HTTP_404_NOT_FOUND, resp.text


async def test_get_organization_by_id(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    org_id = await _create_org(client, admin, "Detail")

    resp = await client.get(f"/organizations/{org_id}", headers=_bearer(admin))
    assert resp.status_code == HTTP_200_OK, resp.text
    body = resp.json()
    assert body["id"] == org_id
    assert body["name"] == "Detail"


async def test_get_missing_org_is_404(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    resp = await client.get(f"/organizations/{uuid4()}", headers=_bearer(admin))
    assert resp.status_code == HTTP_404_NOT_FOUND, resp.text


async def test_spend_rollup_lists_every_team(client: AsyncTestClient) -> None:
    # With no recorded usage the totals are zero, but every team must appear so
    # the detail page shows the full breakdown.
    admin = await _admin(client)
    org_id = await _create_org(client, admin, "Spendy")
    for name in ("Alpha", "Beta"):
        created = await client.post(
            f"/organizations/{org_id}/teams",
            json={"name": name, "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
        assert created.status_code == HTTP_201_CREATED, created.text

    resp = await client.get(f"/organizations/{org_id}/spend", headers=_bearer(admin))
    assert resp.status_code == HTTP_200_OK, resp.text
    body = resp.json()
    assert body["organization_id"] == org_id
    assert body["total_cost"] == 0
    assert {t["name"] for t in body["teams"]} == {"Alpha", "Beta"}
    assert all(t["cost"] == 0 for t in body["teams"])


async def test_spend_rollup_missing_org_is_404(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    resp = await client.get(f"/organizations/{uuid4()}/spend", headers=_bearer(admin))
    assert resp.status_code == HTTP_404_NOT_FOUND, resp.text


async def test_non_admin_cannot_read_detail_or_spend(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    org_id = await _create_org(client, admin, "Private")
    member = await _member(client, admin, "reader@example.com")

    assert (
        await client.get(f"/organizations/{org_id}", headers=_bearer(member))
    ).status_code == HTTP_403_FORBIDDEN
    assert (
        await client.get(f"/organizations/{org_id}/spend", headers=_bearer(member))
    ).status_code == HTTP_403_FORBIDDEN


async def test_non_admin_cannot_mutate(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    org_id = await _create_org(client, admin, "Guarded")
    member = await _member(client, admin, "member@example.com")

    assert (
        await client.patch(
            f"/organizations/{org_id}", json={"name": "Nope"}, headers=_bearer(member)
        )
    ).status_code == HTTP_403_FORBIDDEN
    assert (
        await client.delete(f"/organizations/{org_id}", headers=_bearer(member))
    ).status_code == HTTP_403_FORBIDDEN
