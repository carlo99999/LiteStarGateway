"""Managing a key's spend cap over the API.

The authorization split is the part worth pinning: the team cap is
platform-admin only (a team admin must not raise their own limit), while a *key*
cap is `keys:issue`, because it can only ever make a key spend less — the team
gate runs regardless. Delegating it is safe; delegating the team cap is not.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_400_BAD_REQUEST,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
)
from litestar.testing import AsyncTestClient
from rbac.conftest import _admin, _bearer, _member_token, _team  # type: ignore[import-not-found]

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


async def _key(client: AsyncTestClient, admin: str, team: str, name: str = "k") -> str:
    created = await client.post(f"/teams/{team}/keys", json={"name": name}, headers=_bearer(admin))
    return created.json()["id"]


async def test_set_read_and_remove_a_key_cap(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    key_id = await _key(client, admin, team)
    url = f"/teams/{team}/keys/{key_id}/budget"

    # No cap yet.
    assert (await client.get(url, headers=_bearer(admin))).status_code == HTTP_404_NOT_FOUND

    created = await client.put(
        url, json={"limit_cost": 25.0, "window": "monthly", "mode": "block"}, headers=_bearer(admin)
    )
    assert created.status_code == HTTP_200_OK, created.text
    body = created.json()
    assert (body["limit_cost"], body["window"], body["mode"]) == (25.0, "monthly", "block")
    assert (body["spent"], body["remaining"], body["over_limit"]) == (0.0, 25.0, False)

    fetched = await client.get(url, headers=_bearer(admin))
    assert fetched.json()["limit_cost"] == 25.0

    # A second PUT replaces rather than duplicating.
    replaced = await client.put(
        url, json={"limit_cost": 10.0, "window": "daily"}, headers=_bearer(admin)
    )
    assert (replaced.json()["limit_cost"], replaced.json()["window"]) == (10.0, "daily")
    # Mode defaults to `alert`: adding visibility to a key must not be able to
    # break its owner's workload by accident.
    assert replaced.json()["mode"] == "alert"

    removed = await client.delete(url, headers=_bearer(admin))
    assert removed.status_code == HTTP_204_NO_CONTENT
    assert (await client.get(url, headers=_bearer(admin))).status_code == HTTP_404_NOT_FOUND
    assert (await client.delete(url, headers=_bearer(admin))).status_code == HTTP_404_NOT_FOUND


async def test_a_key_issuer_can_set_a_key_cap(client: AsyncTestClient) -> None:
    # The delegation this design allows: whoever hands out keys can divide the
    # team's budget between them, and cannot enlarge it.
    admin = await _admin(client)
    team = await _team(client, admin)
    key_id = await _key(client, admin, team)
    issuer = await _member_token(client, admin, team, "issuer@example.com", "key-issuer")

    written = await client.put(
        f"/teams/{team}/keys/{key_id}/budget",
        json={"limit_cost": 5.0, "window": "monthly"},
        headers=_bearer(issuer),
    )

    assert written.status_code == HTTP_200_OK, written.text


async def test_a_plain_member_can_neither_read_nor_write(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    key_id = await _key(client, admin, team)
    member = await _member_token(client, admin, team, "member@example.com", "member")
    url = f"/teams/{team}/keys/{key_id}/budget"

    assert (await client.get(url, headers=_bearer(member))).status_code == HTTP_403_FORBIDDEN
    written = await client.put(
        url, json={"limit_cost": 5.0, "window": "monthly"}, headers=_bearer(member)
    )
    assert written.status_code == HTTP_403_FORBIDDEN


async def test_a_billing_viewer_can_read_but_not_write(client: AsyncTestClient) -> None:
    # `budget:read` without `keys:issue` — the same split the team cap uses.
    admin = await _admin(client)
    team = await _team(client, admin)
    key_id = await _key(client, admin, team)
    url = f"/teams/{team}/keys/{key_id}/budget"
    await client.put(url, json={"limit_cost": 5.0, "window": "monthly"}, headers=_bearer(admin))
    viewer = await _member_token(client, admin, team, "viewer@example.com", "billing-viewer")

    assert (await client.get(url, headers=_bearer(viewer))).status_code == HTTP_200_OK
    written = await client.put(
        url, json={"limit_cost": 500.0, "window": "monthly"}, headers=_bearer(viewer)
    )
    assert written.status_code == HTTP_403_FORBIDDEN


async def test_a_key_from_another_team_is_not_found(client: AsyncTestClient) -> None:
    # Resolved through the team, so a foreign key id is a 404 rather than a
    # cross-tenant read or write.
    admin = await _admin(client)
    team, other_team = await _team(client, admin), await _team(client, admin)
    foreign_key = await _key(client, admin, other_team)
    url = f"/teams/{team}/keys/{foreign_key}/budget"

    assert (await client.get(url, headers=_bearer(admin))).status_code == HTTP_404_NOT_FOUND
    written = await client.put(
        url, json={"limit_cost": 5.0, "window": "monthly"}, headers=_bearer(admin)
    )
    assert written.status_code == HTTP_404_NOT_FOUND


@pytest.mark.parametrize(
    "payload",
    [
        {"limit_cost": 0.0, "window": "monthly"},
        {"limit_cost": -1.0, "window": "monthly"},
        {"limit_cost": 5.0, "window": "hourly"},
        {"limit_cost": 5.0, "window": "monthly", "mode": "shout"},
    ],
)
async def test_an_invalid_cap_is_a_bad_request(client: AsyncTestClient, payload: dict) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    key_id = await _key(client, admin, team)

    written = await client.put(
        f"/teams/{team}/keys/{key_id}/budget", json=payload, headers=_bearer(admin)
    )

    assert written.status_code == HTTP_400_BAD_REQUEST, written.text


async def test_rotating_a_capped_key_carries_the_cap_to_its_replacement(
    client: AsyncTestClient,
) -> None:
    """ISSUE-052: rotation issues a new key id, and the cap is keyed by
    `api_key_id`, so nothing carried it over. Routine hygiene therefore turned a
    capped key into an uncapped one with no signal, and the old cap died with the
    old key. Rotation already preserves scope, rate limit, owner and TTL — the
    cap was simply missed when it was added later."""
    admin = await _admin(client)
    team = await _team(client, admin)
    key_id = await _key(client, admin, team)
    capped = await client.put(
        f"/teams/{team}/keys/{key_id}/budget",
        json={"limit_cost": 25.0, "window": "monthly", "mode": "block"},
        headers=_bearer(admin),
    )
    assert capped.status_code == HTTP_200_OK, capped.text

    rotated = await client.post(f"/teams/{team}/keys/{key_id}/rotate", headers=_bearer(admin))
    assert rotated.status_code in (HTTP_200_OK, HTTP_201_CREATED), rotated.text
    new_key_id = rotated.json()["id"]
    assert new_key_id != key_id

    carried = await client.get(f"/teams/{team}/keys/{new_key_id}/budget", headers=_bearer(admin))
    assert carried.status_code == HTTP_200_OK, carried.text
    body = carried.json()
    assert (body["limit_cost"], body["window"], body["mode"]) == (25.0, "monthly", "block")
