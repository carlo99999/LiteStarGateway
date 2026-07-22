"""Global (platform) routers and extending a team router to other teams.

Mirrors test_platform_models: a global router is callable by any team; extend
creates a grant the target team sees; make-global promotes + keeps provenance.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_409_CONFLICT,
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
    # Promotion is safe only when the immutable revision has global deps.
    await _global_model(client, admin, cred, "m")
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
    model = await client.post(
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
    model_id = model.json()["id"]
    extended = await client.post(
        f"/platform/models/{model_id}/extend",
        json={"team_ids": [blue]},
        headers=_b(admin),
    )
    assert extended.status_code == HTTP_201_CREATED, extended.text
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


async def test_grant_pins_revision_and_admin_upgrade_is_audited(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    cred = await _cred(client, admin)
    core = await _org_team(client, admin, "Core")
    blue = await _org_team(client, admin, "Blue")
    source_model = await client.post(
        f"/teams/{core}/models",
        json={
            "name": "same-name",
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "source-model",
        },
        headers=_b(admin),
    )
    source_model_id = source_model.json()["id"]
    target_model = await client.post(
        f"/teams/{blue}/models",
        json={
            "name": "same-name",
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "target-homonym",
        },
        headers=_b(admin),
    )
    assert target_model.status_code == HTTP_201_CREATED
    model_grant = await client.post(
        f"/platform/models/{source_model_id}/extend",
        json={"team_ids": [blue]},
        headers=_b(admin),
    )
    assert model_grant.status_code == HTTP_201_CREATED, model_grant.text

    created = await client.post(
        f"/teams/{core}/routers",
        json={
            "name": "versioned",
            "candidates": [
                {
                    "model_name": "same-name",
                    "quality_tier": "SIMPLE",
                    "description": "revision-one",
                }
            ],
            "default_model": "same-name",
            "strategy": "complexity",
            "strategy_config": {},
        },
        headers=_b(admin),
    )
    assert created.status_code == HTTP_201_CREATED, created.text
    router = created.json()
    assert router["revision_number"] == 1
    assert router["candidates"][0]["model_id"] == source_model_id

    grant_response = await client.post(
        f"/platform/routers/{router['id']}/extend",
        json={"team_ids": [blue]},
        headers=_b(admin),
    )
    assert grant_response.status_code == HTTP_201_CREATED, grant_response.text
    grant = grant_response.json()[0]
    revision_one = grant["revision_id"]

    blocked_delete = await client.delete(f"/teams/{core}/routers/{router['id']}", headers=_b(admin))
    assert blocked_delete.status_code == HTTP_409_CONFLICT

    updated = await client.put(
        f"/teams/{core}/routers/{router['id']}",
        json={
            "name": "versioned",
            "base_revision_id": revision_one,
            "candidates": [
                {
                    "model_name": "same-name",
                    "model_id": source_model_id,
                    "quality_tier": "SIMPLE",
                    "description": "revision-two",
                }
            ],
            "default_model": "same-name",
            "strategy": "complexity",
            "strategy_config": {},
        },
        headers=_b(admin),
    )
    assert updated.status_code == HTTP_200_OK, updated.text
    revision_two = updated.json()["revision_id"]

    revisions = await client.get(f"/platform/routers/{router['id']}/revisions", headers=_b(admin))
    assert [row["revision_number"] for row in revisions.json()] == [2, 1]
    callable_before = await client.get(f"/teams/{blue}/routers/callable", headers=_b(admin))
    pinned = next(row for row in callable_before.json() if row["alias"] == "versioned")
    assert pinned["router"]["revision_id"] == revision_one
    assert pinned["router"]["candidates"][0]["description"] == "revision-one"
    assert pinned["router"]["candidates"][0]["model_id"] == source_model_id

    upgraded = await client.post(
        f"/platform/routers/grants/{grant['id']}/upgrade",
        json={
            "target_revision_id": revision_two,
            "expected_current_revision_id": revision_one,
        },
        headers=_b(admin),
    )
    assert upgraded.status_code == HTTP_201_CREATED, upgraded.text
    assert upgraded.json()["revision_id"] == revision_two

    stale = await client.post(
        f"/platform/routers/grants/{grant['id']}/upgrade",
        json={
            "target_revision_id": revision_one,
            "expected_current_revision_id": revision_one,
        },
        headers=_b(admin),
    )
    assert stale.status_code == HTTP_409_CONFLICT
    events = (await client.get("/audit", headers=_b(admin))).json()
    upgrades = [event for event in events if event["action"] == "router.grant.upgrade"]
    assert len(upgrades) == 1


async def test_extend_requires_separate_active_and_shadow_webhook_ack(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    cred = await _cred(client, admin)
    core = await _org_team(client, admin, "Core")
    blue = await _org_team(client, admin, "Blue")
    await _global_model(client, admin, cred, "global-chat")
    created = await client.post(
        f"/teams/{core}/routers",
        json={
            "name": "egress-router",
            "candidates": [
                {
                    "model_name": "global-chat",
                    "quality_tier": "SIMPLE",
                    "description": "safe",
                }
            ],
            "default_model": "global-chat",
            "strategy": "webhook",
            "strategy_config": {
                "url": "https://active.example/route",
                "shadow": {"url": "https://shadow.example/route"},
            },
            "shadow_strategy": "webhook",
        },
        headers=_b(admin),
    )
    assert created.status_code == HTTP_201_CREATED, created.text
    router_id = created.json()["id"]

    no_ack = await client.post(
        f"/platform/routers/{router_id}/extend",
        json={"team_ids": [blue]},
        headers=_b(admin),
    )
    assert no_ack.status_code == HTTP_400_BAD_REQUEST
    active_only = await client.post(
        f"/platform/routers/{router_id}/extend",
        json={"team_ids": [blue], "ack_active_prompt_egress": True},
        headers=_b(admin),
    )
    assert active_only.status_code == HTTP_400_BAD_REQUEST
    both = await client.post(
        f"/platform/routers/{router_id}/extend",
        json={
            "team_ids": [blue],
            "ack_active_prompt_egress": True,
            "ack_shadow_prompt_egress": True,
        },
        headers=_b(admin),
    )
    assert both.status_code == HTTP_201_CREATED, both.text
    team_delete = await client.delete(f"/teams/{core}", headers=_b(admin))
    assert team_delete.status_code == HTTP_409_CONFLICT
