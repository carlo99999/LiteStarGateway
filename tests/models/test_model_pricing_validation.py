"""ISSUE-022: no management path may configure a negative rate.

A negative rate is not a display bug — `compute_cost` multiplies it directly,
so settlement writes a *credit* into the same ledger the budget gate reads, and
a hard cap stops stopping. The write paths are team create/update (any holder of
`MODELS_MANAGE`, including the team-scoped `model-manager` role) and the
platform create/update for global models; all four go through `ModelService`,
which is where the invariant is enforced.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from _invite_helpers import seed_team_and_invite
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"
MEMBER_PASSWORD = "Sup3r-Secret!"  # pragma: allowlist secret

RATE_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_write_cost_per_token",
    "cache_read_cost_per_token",
    "image_cost_per_image",
)


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="unit-test-salt-key",
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


async def _credential(client: AsyncTestClient, admin: str) -> str:
    resp = await client.post(
        "/credentials",
        json={"name": "cred-openai", "provider": "openai", "values": {"api_key": "x"}},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
    return resp.json()["id"]


async def _team(client: AsyncTestClient, admin: str) -> str:
    org = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin))
    ).json()
    resp = await client.post(
        f"/organizations/{org['id']}/teams",
        json={"name": "Core", "admin_email": ADMIN_EMAIL},
        headers=_bearer(admin),
    )
    return resp.json()["id"]


async def _model_manager_token(client: AsyncTestClient, admin: str, team: str) -> str:
    invite = await seed_team_and_invite(client, admin)
    signup = await client.post(
        "/signup",
        json={"invite_token": invite, "email": "mm@corp.com", "password": MEMBER_PASSWORD},
    )
    assert signup.status_code == HTTP_201_CREATED, signup.text
    added = await client.post(
        f"/teams/{team}/members",
        json={"email": "mm@corp.com", "role": "model-manager"},
        headers=_bearer(admin),
    )
    assert added.status_code == HTTP_201_CREATED, added.text
    return await _login(client, "mm@corp.com", MEMBER_PASSWORD)


def _payload(cred: str, name: str = "fast-chat", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "provider": "openai",
        "credential_id": cred,
        "type": "chat",
        "provider_model_id": "gpt-4o",
        **extra,
    }


# ── Create ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", RATE_FIELDS)
async def test_create_rejects_a_negative_rate_on_every_dimension(
    client: AsyncTestClient, field: str
) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    team = await _team(client, admin)
    negative: dict[str, Any] = {field: -1.0}
    resp = await client.post(
        f"/teams/{team}/models",
        json=_payload(cred, **negative),
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST, resp.text
    assert field in resp.text
    assert (await client.get(f"/teams/{team}/models", headers=_bearer(admin))).json() == []


async def test_create_rejects_a_negative_image_price_entry(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    team = await _team(client, admin)
    resp = await client.post(
        f"/teams/{team}/models",
        json=_payload(cred, image_prices={"1024x1024/hd": -0.08}),
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST, resp.text


async def test_model_manager_cannot_configure_negative_rates(client: AsyncTestClient) -> None:
    # The reported escalation: a team-scoped model-manager is meant to
    # administer deployments, not to hand their team a spend credit.
    admin = await _admin(client)
    cred = await _credential(client, admin)
    team = await _team(client, admin)
    token = await _model_manager_token(client, admin, team)
    resp = await client.post(
        f"/teams/{team}/models",
        json=_payload(
            cred,
            input_cost_per_token=-1.0,
            output_cost_per_token=-2.0,
            cache_write_cost_per_token=-3.0,
            cache_read_cost_per_token=-4.0,
            image_cost_per_image=-5.0,
        ),
        headers=_bearer(token),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST, resp.text


async def test_create_accepts_zero_and_positive_rates(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    team = await _team(client, admin)
    resp = await client.post(
        f"/teams/{team}/models",
        json=_payload(
            cred,
            input_cost_per_token=0.0,
            output_cost_per_token=0.000015,
            cache_read_cost_per_token=0.0,
            image_cost_per_image=0.04,
            image_prices={"1024x1024/hd": 0.08},
        ),
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text


async def test_platform_global_model_create_rejects_negative_rate(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    resp = await client.post(
        "/platform/models",
        json=_payload(cred, name="global-chat", input_cost_per_token=-1.0),
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST, resp.text


# ── Update ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", RATE_FIELDS)
async def test_update_rejects_a_negative_rate_and_leaves_the_model_intact(
    client: AsyncTestClient, field: str
) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    team = await _team(client, admin)
    created = await client.post(
        f"/teams/{team}/models",
        json=_payload(cred, input_cost_per_token=0.000005),
        headers=_bearer(admin),
    )
    model_id = created.json()["id"]

    resp = await client.patch(
        f"/teams/{team}/models/{model_id}",
        json={field: -1.0},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST, resp.text
    listed = (await client.get(f"/teams/{team}/models", headers=_bearer(admin))).json()
    current = next(m for m in listed if m["id"] == model_id)
    assert current["input_cost_per_token"] == 0.000005
    assert current[field] in (None, 0.000005)


async def test_update_rejects_a_negative_image_price_entry(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    team = await _team(client, admin)
    model_id = (
        await client.post(f"/teams/{team}/models", json=_payload(cred), headers=_bearer(admin))
    ).json()["id"]
    resp = await client.patch(
        f"/teams/{team}/models/{model_id}",
        json={"image_prices": {"1024x1024/hd": -0.08}},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST, resp.text


async def test_partial_update_that_does_not_touch_pricing_still_works(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    team = await _team(client, admin)
    model_id = (
        await client.post(
            f"/teams/{team}/models",
            json=_payload(cred, input_cost_per_token=0.000005),
            headers=_bearer(admin),
        )
    ).json()["id"]
    resp = await client.patch(
        f"/teams/{team}/models/{model_id}",
        json={"provider_model_id": "gpt-4o-mini"},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    assert resp.json()["provider_model_id"] == "gpt-4o-mini"
