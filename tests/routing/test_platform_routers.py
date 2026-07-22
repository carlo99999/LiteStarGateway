"""Global (platform) routers and extending a team router to other teams.

Mirrors test_platform_models: a global router is callable by any team; extend
creates a grant the target team sees; make-global promotes + keeps provenance.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED
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
    async with AsyncTestClient(app=create_app(settings)) as c:
        yield c


def _b(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


async def _admin(client: AsyncTestClient) -> str:
    r = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    return r.json()["access_token"]


async def _cred(client: AsyncTestClient, admin: str) -> str:
    r = await client.post(
        "/credentials",
        json={"name": "c", "provider": "openai", "values": {"api_key": "x"}},
        headers=_b(admin),
    )
    return r.json()["id"]


async def _org_team(client: AsyncTestClient, admin: str, name: str) -> str:
    org = (await client.post("/organizations", json={"name": "Acme"}, headers=_b(admin))).json()[
        "id"
    ]
    return (
        await client.post(
            f"/organizations/{org}/teams",
            json={"name": name, "admin_email": ADMIN_EMAIL},
            headers=_b(admin),
        )
    ).json()["id"]


async def _global_model(client: AsyncTestClient, admin: str, cred: str, name: str) -> None:
    r = await client.post(
        "/platform/models",
        json={
            "name": name,
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
        },
        headers=_b(admin),
    )
    assert r.status_code == HTTP_201_CREATED, r.text


async def _global_router(client: AsyncTestClient, admin: str, name: str, candidate: str) -> str:
    r = await client.post(
        "/platform/routers",
        json={
            "name": name,
            "candidates": [{"model_name": candidate, "quality_tier": "SIMPLE", "description": "t"}],
            "default_model": candidate,
            "strategy": "complexity",
            "strategy_config": {},
        },
        headers=_b(admin),
    )
    assert r.status_code in (HTTP_200_OK, HTTP_201_CREATED), r.text
    return r.json()["id"]


async def _team_key(client: AsyncTestClient, admin: str, team: str) -> str:
    return (await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_b(admin))).json()[
        "plaintext"
    ]


async def test_global_router_is_callable_by_a_team(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _cred(client, admin)
    team = await _org_team(client, admin, "Core")
    await _global_model(client, admin, cred, "g-chat")  # candidate must be reachable
    await _global_router(client, admin, "smart-global", "g-chat")
    key = await _team_key(client, admin, team)

    models = (await client.get("/v1/models", headers=_b(key))).json()["data"]
    assert "smart-global" in {m["id"] for m in models}


async def test_make_router_global_keeps_provenance(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _cred(client, admin)
    core = await _org_team(client, admin, "Core")
    # A team router over a team model.
    await client.post(
        f"/teams/{core}/models",
        json={
            "name": "m",
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
        },
        headers=_b(admin),
    )
    rid = (
        await client.post(
            f"/teams/{core}/routers",
            json={
                "name": "r",
                "candidates": [{"model_name": "m", "quality_tier": "SIMPLE", "description": "t"}],
                "default_model": "m",
                "strategy": "complexity",
                "strategy_config": {},
            },
            headers=_b(admin),
        )
    ).json()["id"]

    resp = await client.post(f"/platform/routers/{rid}/make-global", headers=_b(admin))
    assert resp.status_code in (HTTP_200_OK, HTTP_201_CREATED), resp.text
    body = resp.json()
    assert body["team_id"] is None
    assert body["origin_team_id"] == core


async def test_extend_router_to_team(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _cred(client, admin)
    core = await _org_team(client, admin, "Core")
    blue = await _org_team(client, admin, "Blue")
    await client.post(
        f"/teams/{core}/models",
        json={
            "name": "m",
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
        },
        headers=_b(admin),
    )
    rid = (
        await client.post(
            f"/teams/{core}/routers",
            json={
                "name": "shared-router",
                "candidates": [{"model_name": "m", "quality_tier": "SIMPLE", "description": "t"}],
                "default_model": "m",
                "strategy": "complexity",
                "strategy_config": {},
            },
            headers=_b(admin),
        )
    ).json()["id"]

    grants = (
        await client.post(
            f"/platform/routers/{rid}/extend", json={"team_ids": [blue]}, headers=_b(admin)
        )
    ).json()
    assert len(grants) == 1
    assert grants[0]["alias"] == "shared-router"

    callable_rows = (await client.get(f"/teams/{blue}/routers/callable", headers=_b(admin))).json()
    shared = next(r for r in callable_rows if r["alias"] == "shared-router")
    assert shared["origin"] == "extended"
    assert shared["source_team_id"] == core
