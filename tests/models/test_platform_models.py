"""Global (platform) models and extending a team model to other teams.

Covers the resolution rules end to end through the HTTP surface: a global model
is callable by any team; an extended model is callable by the target team under
its (possibly disambiguated) alias; and the priority own > extended > global
holds, with `-<team>` / `-global` suffixes on a clash.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_401_UNAUTHORIZED,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"
SALT_KEY = "unit-test-salt-key"


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=SALT_KEY,
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin(client: AsyncTestClient) -> str:
    resp = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    return resp.json()["access_token"]


async def _credential(client: AsyncTestClient, admin: str) -> str:
    resp = await client.post(
        "/credentials",
        json={"name": "c-openai", "provider": "openai", "values": {"api_key": "x"}},
        headers=_bearer(admin),
    )
    return resp.json()["id"]


async def _team(client: AsyncTestClient, admin: str, org: str, name: str) -> str:
    resp = await client.post(
        f"/organizations/{org}/teams",
        json={"name": name, "admin_email": ADMIN_EMAIL},
        headers=_bearer(admin),
    )
    return resp.json()["id"]


async def _team_model(client: AsyncTestClient, admin: str, team: str, cred: str, name: str) -> str:
    resp = await client.post(
        f"/teams/{team}/models",
        json={
            "name": name,
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
        },
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
    return resp.json()["id"]


async def _global_model(client: AsyncTestClient, admin: str, cred: str, name: str) -> str:
    resp = await client.post(
        "/platform/models",
        json={
            "name": name,
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
        },
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
    return resp.json()["id"]


async def _team_key(client: AsyncTestClient, admin: str, team: str) -> str:
    resp = await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    return resp.json()["plaintext"]


async def _catalog(client: AsyncTestClient, key: str) -> set[str]:
    """The set of model ids a team can call, from GET /v1/models."""
    resp = await client.get("/v1/models", headers=_bearer(key))
    assert resp.status_code == HTTP_200_OK, resp.text
    return {entry["id"] for entry in resp.json()["data"]}


async def _org(client: AsyncTestClient, admin: str) -> str:
    resp = await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin))
    return resp.json()["id"]


async def test_global_model_is_callable_by_a_team(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    team = await _team(client, admin, org, "Core")
    await _global_model(client, admin, cred, "shared-gpt")
    key = await _team_key(client, admin, team)

    # The team never created "shared-gpt" but can call it — it's global.
    assert "shared-gpt" in await _catalog(client, key)


async def test_extend_to_team_without_collision_uses_plain_name(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    core = await _team(client, admin, org, "Core")
    blue = await _team(client, admin, org, "Blue")
    model_id = await _team_model(client, admin, core, cred, "gpt-4o")

    resp = await client.post(
        f"/platform/models/{model_id}/extend",
        json={"team_ids": [blue]},
        headers=_bearer(admin),
    )
    assert resp.status_code in (HTTP_200_OK, HTTP_201_CREATED), resp.text
    grants = resp.json()
    assert len(grants) == 1
    assert grants[0]["alias"] == "gpt-4o"

    blue_key = await _team_key(client, admin, blue)
    assert "gpt-4o" in await _catalog(client, blue_key)


async def test_extend_collision_suffixes_with_source_team(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    core = await _team(client, admin, org, "Core")
    blue = await _team(client, admin, org, "Blue")
    # Blue already has its own "gpt-4o".
    await _team_model(client, admin, blue, cred, "gpt-4o")
    core_model = await _team_model(client, admin, core, cred, "gpt-4o")

    resp = await client.post(
        f"/platform/models/{core_model}/extend",
        json={"team_ids": [blue]},
        headers=_bearer(admin),
    )
    assert resp.json()[0]["alias"] == "gpt-4o-core"

    blue_key = await _team_key(client, admin, blue)
    catalog = await _catalog(client, blue_key)
    assert "gpt-4o" in catalog  # Blue's own
    assert "gpt-4o-core" in catalog  # the extended one


async def test_global_collision_exposes_dash_global(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    team = await _team(client, admin, org, "Core")
    await _global_model(client, admin, cred, "gpt-4o")
    await _team_model(client, admin, team, cred, "gpt-4o")  # team shadows the global
    key = await _team_key(client, admin, team)

    catalog = await _catalog(client, key)
    assert "gpt-4o" in catalog  # the team's own wins the plain name
    assert "gpt-4o-global" in catalog  # the global is still reachable, disambiguated


async def test_unextend_removes_it_from_the_target(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    core = await _team(client, admin, org, "Core")
    blue = await _team(client, admin, org, "Blue")
    model_id = await _team_model(client, admin, core, cred, "gpt-4o")
    grant_id = (
        await client.post(
            f"/platform/models/{model_id}/extend",
            json={"team_ids": [blue]},
            headers=_bearer(admin),
        )
    ).json()[0]["id"]

    blue_key = await _team_key(client, admin, blue)
    assert "gpt-4o" in await _catalog(client, blue_key)

    resp = await client.delete(f"/platform/models/grants/{grant_id}", headers=_bearer(admin))
    assert resp.status_code in (HTTP_200_OK, 204)
    assert "gpt-4o" not in await _catalog(client, blue_key)


async def test_callable_view_shows_own_extended_and_global(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    core = await _team(client, admin, org, "Core")
    blue = await _team(client, admin, org, "Blue")
    await _global_model(client, admin, cred, "g")
    await _team_model(client, admin, core, cred, "own-m")
    blue_model = await _team_model(client, admin, blue, cred, "from-blue")
    await client.post(
        f"/platform/models/{blue_model}/extend",
        json={"team_ids": [core]},
        headers=_bearer(admin),
    )

    resp = await client.get(f"/teams/{core}/models/callable", headers=_bearer(admin))
    assert resp.status_code == HTTP_200_OK, resp.text
    by_alias = {row["alias"]: row for row in resp.json()}

    assert by_alias["own-m"]["origin"] == "own"
    assert by_alias["g"]["origin"] == "global"
    assert by_alias["from-blue"]["origin"] == "extended"
    assert by_alias["from-blue"]["source_team_id"] == blue


async def test_platform_routes_require_admin(client: AsyncTestClient) -> None:
    # No token → the platform-admin guard rejects before any work.
    resp = await client.get("/platform/models")
    assert resp.status_code == HTTP_401_UNAUTHORIZED
